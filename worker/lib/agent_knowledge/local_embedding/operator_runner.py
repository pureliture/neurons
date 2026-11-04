"""Approval-gated, aggregate-only runner for local embedding backfills."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .client import DEFAULT_MAX_RETRIES, MAX_RETRY_DELAY_SECONDS
from .profile import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL_ALIAS,
    LOCAL_EMBEDDING_PROFILE_ID,
    LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
)

OPERATOR_REPORT_SCHEMA_VERSION = "local_embedding_operator_report.v2"
APPROVAL_SCHEMA_VERSION = "local_embedding_backfill_approval.v3"
MAX_BATCH_SIZE = 32
MAX_BATCHES = 100
MAX_BATCHES_PER_GROUP = 100
MAX_TIME_LIMIT_SECONDS = 600.0
MAX_LEASE_SECONDS = 300
DEFAULT_BATCH_SIZE = 1
DEFAULT_LEASE_SECONDS = 120
LOCAL_EMBEDDING_CLIENT_TIMEOUT_SECONDS = 30.0
LOCAL_EMBEDDING_CLIENT_MAX_RETRIES = DEFAULT_MAX_RETRIES
LOCAL_EMBEDDING_CLIENT_MAX_RETRY_AFTER_SECONDS = MAX_RETRY_DELAY_SECONDS
LOCAL_EMBEDDING_WRITE_MARGIN_SECONDS = 15.0
# Three worst-case UTF-8 chunks (each may lose up to three boundary bytes)
# fit in one canonical request, including JSON escaping of control characters.
PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES = 3 * (LOCAL_MODEL2VEC_MAX_CHUNK_BYTES - 3)
MAX_RECEIPT_PAGE_SIZE = 256
MAX_RECEIPT_PAGES = 10
MAX_RECEIPT_ROWS = 5000
MAX_RECEIPT_SECONDS = 600.0
MAX_NEO4J_TARGET_GROUPS = 4096
MAX_APPROVAL_ARTIFACT_BYTES = 64 * 1024


class ApprovalValidationError(ValueError):
    """A sanitized approval artifact validation failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class BackfillPlan:
    lane: str
    phase: str | None
    vector_scope: str | None
    batch_size: int
    max_batches: int
    max_batches_per_group: int
    max_seconds: float
    lease_seconds: int
    receipt_page_size: int
    receipt_max_pages: int
    receipt_max_rows: int
    receipt_max_seconds: float
    profile_id: str
    model: str
    dimension: int
    target_fingerprint: str
    selection_fingerprint: str
    minimum_lease_seconds: int = 0
    max_embedding_input_bytes: int = 0
    receipt_checkpoint_path: str = ""
    receipt_run_id: str = ""
    receipt_source_identity: str = ""

    @property
    def limits(self) -> dict[str, int | float]:
        limits: dict[str, int | float] = {
            "batch_size": self.batch_size,
            "max_batches": self.max_batches,
            "max_batches_per_group": self.max_batches_per_group,
            "between_batch_budget_seconds": self.max_seconds,
            "lease_seconds": self.lease_seconds,
            "receipt_page_size": self.receipt_page_size,
            "receipt_max_pages": self.receipt_max_pages,
            "receipt_max_rows": self.receipt_max_rows,
            "receipt_between_page_budget_seconds": min(
                self.receipt_max_seconds, self.max_seconds
            ),
        }
        if self.lane == "pg" and self.phase == "vectors":
            limits.update(
                {
                    "minimum_lease_seconds": self.minimum_lease_seconds,
                    "max_embedding_input_bytes": self.max_embedding_input_bytes,
                    "provider_timeout_seconds": min(
                        LOCAL_EMBEDDING_CLIENT_TIMEOUT_SECONDS, self.max_seconds
                    ),
                }
            )
        return limits


@dataclass(frozen=True)
class _RuntimeTarget:
    pg_dsn: str = ""
    couchdb_url: str = ""
    couchdb_db: str = "transcript_source"
    couchdb_user: str = ""
    couchdb_password: str = ""
    neo4j_uri: str = ""
    neo4j_database: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""


class _CountingLocalEmbedder:
    """Synchronous adapter used by both backfill lanes and their aggregate report."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.model_calls = 0
        self.base_url = str(getattr(client, "_base_url", "") or "")
        self._max_input_bytes: int | None = None

    def set_max_input_bytes(self, maximum: int) -> None:
        self._max_input_bytes = _positive_int(
            "embedding input byte ceiling",
            maximum,
            PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES,
        )

    def _validate_input_bytes(self, texts: Sequence[str]) -> None:
        if self._max_input_bytes is None:
            return
        try:
            input_bytes = sum(len(text.encode("utf-8", errors="strict")) for text in texts)
        except (AttributeError, UnicodeEncodeError):
            raise ValueError("embedding_input_invalid") from None
        if input_bytes > self._max_input_bytes:
            raise ValueError("embedding_input_exceeds_lease_bound")

    def embed(self, text: str) -> list[float]:
        self._validate_input_bytes([text])
        self.model_calls += 1
        result = self._client.embed_document(text)
        return list(result.vector)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self._validate_input_bytes(texts)
        self.model_calls += 1
        return [list(result.vector) for result in self._client.embed_documents(list(texts))]

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def _canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalise_group_ids(group_ids: Sequence[str] | None) -> list[str] | None:
    if group_ids is None:
        return None
    groups = sorted({str(value).strip() for value in group_ids if str(value).strip()})
    if len(groups) > MAX_NEO4J_TARGET_GROUPS:
        raise ValueError("group selection exceeds the hard bound")
    return groups


def _positive_int(name: str, value: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive bounded integer")
    result = int(value)
    if not 1 <= result <= maximum:
        raise ValueError(f"{name} exceeds the hard bound")
    return result


def _positive_seconds(name: str, value: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and positive")
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= maximum:
        raise ValueError(f"{name} exceeds the hard bound")
    return result


def build_backfill_plan(
    *,
    lane: str,
    phase: str | None = None,
    vector_scope: str | None = None,
    target_identity: Mapping[str, Any] | None = None,
    group_ids: Sequence[str] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = 10,
    max_batches_per_group: int = 10,
    max_seconds: float = 300,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    receipt_page_size: int = 64,
    receipt_max_pages: int = 10,
    receipt_max_rows: int = 500,
    receipt_max_seconds: float = 60,
    receipt_checkpoint_path: str | Path | None = None,
    receipt_run_id: str = "",
    receipt_source_identity: str = "",
) -> BackfillPlan:
    """Create a no-I/O plan and hard-reject unbounded execution limits."""
    lane_value = str(lane or "").strip().lower()
    if lane_value not in {"pg", "neo4j"}:
        raise ValueError("lane must be pg or neo4j")
    phase_value = str(phase or ("vectors" if lane_value == "pg" else "")).strip().lower()
    if lane_value == "pg" and phase_value not in {"vectors", "receipts"}:
        raise ValueError("phase must be vectors or receipts for the pg lane")
    vector_scope_value = str(vector_scope or "").strip().lower() or None
    if lane_value == "pg" and phase_value == "vectors":
        if vector_scope_value not in {"cards", "sessions", "all"}:
            raise ValueError("vector scope must be cards, sessions, or all")
    elif vector_scope_value is not None:
        raise ValueError("vector scope is only supported by the pg vectors phase")
    if lane_value != "pg" and (phase_value or receipt_run_id or receipt_source_identity):
        raise ValueError("phase and receipt bindings are only supported by the pg lane")
    normalized_groups = _normalise_group_ids(group_ids)
    if lane_value == "pg" and normalized_groups:
        raise ValueError("group selection is only supported by the neo4j lane")
    checkpoint_path = _canonical_receipt_checkpoint_path(receipt_checkpoint_path)
    if lane_value != "pg" and checkpoint_path:
        raise ValueError("receipt checkpoint is only supported by the pg lane")
    run_id = str(receipt_run_id or "").strip()
    source_identity = str(receipt_source_identity or "").strip()
    if lane_value == "pg" and phase_value == "vectors":
        if checkpoint_path or run_id or source_identity:
            raise ValueError("receipt bindings are not supported by the vectors phase")
    if lane_value == "pg" and phase_value == "receipts":
        if not checkpoint_path:
            raise ValueError("receipt checkpoint is required for the receipts phase")
        if len(run_id) > 128 or not run_id or any(char.isspace() for char in run_id):
            raise ValueError("receipt run id is invalid")
        if len(source_identity) != 71 or not source_identity.startswith("sha256:"):
            raise ValueError("receipt source identity is invalid")
        if any(char not in "0123456789abcdef" for char in source_identity[7:]):
            raise ValueError("receipt source identity is invalid")

    target = dict(target_identity or {})
    target_fingerprint = _canonical_fingerprint(
        {
            "lane": lane_value,
            "phase": phase_value or None,
            "vector_scope": vector_scope_value,
            "target": target,
            "receipt_checkpoint_path": checkpoint_path or None,
            "receipt_run_id": run_id or None,
            "receipt_source_identity": source_identity or None,
        }
    )
    selection_fingerprint = _canonical_fingerprint(
        {
            "lane": lane_value,
            "scope": (
                vector_scope_value
                if lane_value == "pg" and phase_value == "vectors"
                else "receipt_snapshot"
                if lane_value == "pg" and phase_value == "receipts"
                else "all_groups"
                if normalized_groups is None
                else normalized_groups
            ),
            "profile_id": LOCAL_EMBEDDING_PROFILE_ID,
        }
    )
    normalized_batch_size = _positive_int("batch size", batch_size, MAX_BATCH_SIZE)
    normalized_max_seconds = _positive_seconds(
        "time budget", max_seconds, MAX_TIME_LIMIT_SECONDS
    )
    normalized_lease_seconds = _positive_int("lease seconds", lease_seconds, MAX_LEASE_SECONDS)
    minimum_lease_seconds = (
        _minimum_pg_lease_seconds(
            batch_size=normalized_batch_size,
            max_seconds=normalized_max_seconds,
        )
        if lane_value == "pg" and phase_value == "vectors"
        else 0
    )
    if (
        lane_value == "pg"
        and phase_value == "vectors"
        and normalized_lease_seconds < minimum_lease_seconds
    ):
        raise ValueError("lease is shorter than the canonical provider batch bound")

    return BackfillPlan(
        lane=lane_value,
        phase=phase_value or None,
        vector_scope=vector_scope_value,
        batch_size=normalized_batch_size,
        max_batches=_positive_int("batch ceiling", max_batches, MAX_BATCHES),
        max_batches_per_group=_positive_int(
            "per-group batch ceiling", max_batches_per_group, MAX_BATCHES_PER_GROUP
        ),
        max_seconds=normalized_max_seconds,
        lease_seconds=normalized_lease_seconds,
        receipt_page_size=_positive_int(
            "receipt page size", receipt_page_size, MAX_RECEIPT_PAGE_SIZE
        ),
        receipt_max_pages=_positive_int("receipt page ceiling", receipt_max_pages, MAX_RECEIPT_PAGES),
        receipt_max_rows=_positive_int("receipt row ceiling", receipt_max_rows, MAX_RECEIPT_ROWS),
        receipt_max_seconds=_positive_seconds(
            "receipt time ceiling", receipt_max_seconds, MAX_RECEIPT_SECONDS
        ),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        model=LOCAL_EMBEDDING_MODEL_ALIAS,
        dimension=LOCAL_EMBEDDING_DIM,
        target_fingerprint=target_fingerprint,
        selection_fingerprint=selection_fingerprint,
        minimum_lease_seconds=minimum_lease_seconds,
        max_embedding_input_bytes=(
            PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES if lane_value == "pg" else 0
        ),
        receipt_checkpoint_path=checkpoint_path,
        receipt_run_id=run_id,
        receipt_source_identity=source_identity,
    )


def _minimum_pg_lease_seconds(*, batch_size: int, max_seconds: float) -> int:
    """Bound serial jobs by the canonical client's request, retries, and write margin."""
    timeout = min(LOCAL_EMBEDDING_CLIENT_TIMEOUT_SECONDS, max_seconds)
    provider_attempt_seconds = timeout * (LOCAL_EMBEDDING_CLIENT_MAX_RETRIES + 1)
    retry_wait_seconds = (
        LOCAL_EMBEDDING_CLIENT_MAX_RETRIES
        * LOCAL_EMBEDDING_CLIENT_MAX_RETRY_AFTER_SECONDS
    )
    per_job_seconds = (
        provider_attempt_seconds + retry_wait_seconds + LOCAL_EMBEDDING_WRITE_MARGIN_SECONDS
    )
    return math.ceil(per_job_seconds * batch_size)


def _canonical_receipt_checkpoint_path(path_value: str | Path | None) -> str:
    if path_value is None or not str(path_value).strip():
        return ""
    return os.path.abspath(os.path.expanduser(str(path_value).strip()))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _prepare_receipt_checkpoint_path(
    path_value: str | Path,
    *,
    home: Path | None = None,
) -> Path:
    """Prepare and validate a durable private checkpoint location."""
    planned_path = Path(_canonical_receipt_checkpoint_path(path_value))
    home_path = (home or Path.home()).resolve()
    resolved_path = planned_path.resolve(strict=False)
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        not planned_path.is_absolute()
        or resolved_path != planned_path
        or not _is_relative_to(resolved_path, home_path)
        or _is_relative_to(resolved_path, temporary_root)
        or "Caches" in resolved_path.parts
        or ".cache" in resolved_path.parts
    ):
        raise ValueError("receipt_checkpoint_storage_invalid")

    missing_directories: list[Path] = []
    directory = resolved_path.parent
    while not directory.exists():
        missing_directories.append(directory)
        parent = directory.parent
        if parent == directory:
            raise ValueError("receipt_checkpoint_storage_invalid")
        directory = parent
    if not directory.is_dir():
        raise ValueError("receipt_checkpoint_storage_invalid")
    for missing_directory in reversed(missing_directories):
        try:
            missing_directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            os.chmod(missing_directory, 0o700, follow_symlinks=False)
        except OSError:
            raise ValueError("receipt_checkpoint_storage_not_private") from None

    parent_stat = resolved_path.parent.stat()
    if parent_stat.st_uid != os.getuid() or (parent_stat.st_mode & 0o777) != 0o700:
        raise ValueError("receipt_checkpoint_storage_not_private")
    if resolved_path.exists() or resolved_path.is_symlink():
        if resolved_path.is_symlink() or not resolved_path.is_file():
            raise ValueError("receipt_checkpoint_storage_invalid")
        checkpoint_stat = resolved_path.stat()
        if checkpoint_stat.st_uid != os.getuid() or (checkpoint_stat.st_mode & 0o777) != 0o600:
            raise ValueError("receipt_checkpoint_storage_not_private")
    return resolved_path


def _approval_target(plan: BackfillPlan) -> dict[str, Any]:
    return {
        "lane": plan.lane,
        "phase": plan.phase,
        "vector_scope": plan.vector_scope,
        "target_fingerprint": plan.target_fingerprint,
        "selection_fingerprint": plan.selection_fingerprint,
        "profile_id": plan.profile_id,
        "model": plan.model,
        "dimension": plan.dimension,
        "receipt_checkpoint_path": plan.receipt_checkpoint_path or None,
        "receipt_run_id": plan.receipt_run_id or None,
        "receipt_source_identity": plan.receipt_source_identity or None,
    }


def _validate_approval_limits(limits: Any) -> None:
    if not isinstance(limits, dict):
        raise ApprovalValidationError("approval_limits_invalid")
    maximums: dict[str, int | float] = {
        "batch_size": MAX_BATCH_SIZE,
        "max_batches": MAX_BATCHES,
        "max_batches_per_group": MAX_BATCHES_PER_GROUP,
        "between_batch_budget_seconds": MAX_TIME_LIMIT_SECONDS,
        "lease_seconds": MAX_LEASE_SECONDS,
        "receipt_page_size": MAX_RECEIPT_PAGE_SIZE,
        "receipt_max_pages": MAX_RECEIPT_PAGES,
        "receipt_max_rows": MAX_RECEIPT_ROWS,
        "receipt_between_page_budget_seconds": MAX_RECEIPT_SECONDS,
    }
    optional_maximums: dict[str, int | float] = {
        "minimum_lease_seconds": MAX_LEASE_SECONDS,
        "max_embedding_input_bytes": PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES,
        "provider_timeout_seconds": LOCAL_EMBEDDING_CLIENT_TIMEOUT_SECONDS,
    }
    for key, maximum in maximums.items():
        value = limits.get(key)
        if isinstance(value, bool):
            raise ApprovalValidationError("approval_limits_invalid")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApprovalValidationError("approval_limits_invalid")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric <= 0:
            raise ApprovalValidationError("approval_limits_invalid")
        if numeric > maximum:
            raise ApprovalValidationError("approval_limit_exceeded")
    for key, maximum in optional_maximums.items():
        if key not in limits:
            continue
        value = limits[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApprovalValidationError("approval_limits_invalid")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric <= 0:
            raise ApprovalValidationError("approval_limits_invalid")
        if numeric > maximum:
            raise ApprovalValidationError("approval_limit_exceeded")


def validate_approval_artifact(
    payload: Any,
    *,
    plan: BackfillPlan,
    command_argv: Sequence[str],
) -> None:
    """Validate operator approval before constructing any runtime client."""
    if not isinstance(payload, dict):
        raise ApprovalValidationError("approval_artifact_invalid")
    if (
        payload.get("schema_version") != APPROVAL_SCHEMA_VERSION
        or payload.get("operation") != "local_embedding_backfill"
    ):
        raise ApprovalValidationError("approval_schema_mismatch")
    approval = payload.get("operator_approval")
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        raise ApprovalValidationError("operator_approval_missing")
    command = payload.get("command")
    approved_argv = command.get("argv") if isinstance(command, dict) else None
    if not isinstance(approved_argv, list) or approved_argv != list(command_argv):
        raise ApprovalValidationError("approval_argv_mismatch")
    if payload.get("redaction_required") is not True:
        raise ApprovalValidationError("approval_redaction_required")
    rollback = payload.get("rollback_or_abort_criteria")
    if not (isinstance(rollback, str) and rollback.strip()) and not (
        isinstance(rollback, list) and rollback
    ):
        raise ApprovalValidationError("approval_abort_criteria_missing")
    if payload.get("time_budget_semantics") != "between_batches_and_receipt_pages_only":
        raise ApprovalValidationError("approval_time_budget_semantics_mismatch")
    if payload.get("target") != _approval_target(plan):
        raise ApprovalValidationError("approval_target_mismatch")

    limits = payload.get("limits")
    _validate_approval_limits(limits)
    if limits != plan.limits:
        raise ApprovalValidationError("approval_limits_mismatch")


def _resolve_runtime_target(environ: Mapping[str, str]) -> _RuntimeTarget:
    def first(*names: str) -> str:
        for name in names:
            value = str(environ.get(name) or "").strip()
            if value:
                return value
        return ""

    return _RuntimeTarget(
        pg_dsn=first("NEURON_LBRAIN_PGVECTOR_DSN", "LLM_BRAIN_PGVECTOR_DSN", "NEURON_LEDGER_PG_DSN"),
        couchdb_url=first("COUCHDB_URL"),
        couchdb_db=first("COUCHDB_DB") or "transcript_source",
        couchdb_user=first("COUCHDB_USER"),
        couchdb_password=first("COUCHDB_PASSWORD"),
        neo4j_uri=first("LLM_BRAIN_NEO4J_URI", "NEO4J_URI"),
        neo4j_database=first("LLM_BRAIN_NEO4J_DATABASE", "NEO4J_DATABASE"),
        neo4j_user=first("NEO4J_USER", "NEO4J_USERNAME"),
        neo4j_password=first("NEO4J_PASSWORD"),
        embedding_base_url=first("LLM_BRAIN_EMBEDDING_BASE_URL"),
        embedding_model=first("LLM_BRAIN_EMBEDDING_MODEL"),
        embedding_api_key=first("LLM_BRAIN_EMBEDDING_API_KEY"),
    )


def _connection_fields(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        tokens = shlex.split(value)
    except ValueError:
        return fields
    for token in tokens:
        key, separator, item = token.partition("=")
        if separator and key:
            fields[key.casefold()] = item
    return fields


def _safe_endpoint_identity(value: str, *, default_scheme: str = "") -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme and (parsed.netloc or parsed.scheme.casefold() in {"postgres", "postgresql"}):
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "local").casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        return f"{scheme}://{host}{f':{port}' if port is not None else ''}"

    fields = _connection_fields(raw)
    if fields:
        host = (fields.get("host") or fields.get("hostaddr") or "local").casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = fields.get("port", "").strip()
        scheme = default_scheme or "postgresql"
        return f"{scheme}://{host}{f':{port}' if port else ''}"

    if default_scheme:
        endpoint = raw.split("/", 1)[0]
        return f"{default_scheme.casefold()}://{endpoint.casefold()}"
    return ""


def _database_identity(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.path:
        return unquote(parsed.path.strip("/").split("/", 1)[0])
    fields = _connection_fields(raw)
    return fields.get("dbname", fields.get("database", ""))


def _required_credentials_present(
    lane: str,
    target: _RuntimeTarget,
    *,
    phase: str | None = None,
) -> bool:
    if lane == "pg" and phase == "receipts":
        target_ready = bool(target.pg_dsn and target.couchdb_url)
    elif lane == "pg":
        target_ready = bool(target.pg_dsn and target.embedding_base_url)
    else:
        target_ready = bool(target.pg_dsn and target.embedding_base_url and target.neo4j_uri)
    couch_auth_ready = lane != "pg" or phase != "receipts" or not (
        target.couchdb_user or target.couchdb_password
    ) or bool(target.couchdb_user and target.couchdb_password)
    neo4j_auth_ready = lane != "neo4j" or not (
        target.neo4j_user or target.neo4j_password
    ) or bool(target.neo4j_user and target.neo4j_password)
    return target_ready and couch_auth_ready and neo4j_auth_ready


def _resolve_target_identity(
    lane: str,
    environ: Mapping[str, str],
    *,
    phase: str | None = None,
) -> dict[str, Any]:
    target = _resolve_runtime_target(environ)
    identity: dict[str, Any] = {
        "pg_endpoint": _safe_endpoint_identity(target.pg_dsn, default_scheme="postgresql"),
        "pg_database": _database_identity(target.pg_dsn),
        "credentials_present": _required_credentials_present(lane, target, phase=phase),
    }
    if lane == "pg" and phase == "receipts":
        identity.update(
            {
                "couchdb_endpoint": _safe_endpoint_identity(target.couchdb_url),
                "couchdb_database": target.couchdb_db,
            }
        )
    elif lane == "pg":
        identity.update(
            {
                "embedding_endpoint": _safe_endpoint_identity(target.embedding_base_url),
                "embedding_model": target.embedding_model or LOCAL_EMBEDDING_MODEL_ALIAS,
            }
        )
    else:
        identity.update(
            {
                "embedding_endpoint": _safe_endpoint_identity(target.embedding_base_url),
                "embedding_model": target.embedding_model or LOCAL_EMBEDDING_MODEL_ALIAS,
                "neo4j_endpoint": _safe_endpoint_identity(target.neo4j_uri, default_scheme="neo4j"),
                "neo4j_database": target.neo4j_database,
            }
        )
    return identity


def _parse_approval_file(path_value: str) -> dict[str, Any]:
    try:
        path = Path(path_value)
        if not path.is_file() or path.stat().st_size > MAX_APPROVAL_ARTIFACT_BYTES:
            raise ApprovalValidationError("approval_artifact_unavailable")
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except ApprovalValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ApprovalValidationError("approval_artifact_unavailable") from None
    if not isinstance(payload, dict):
        raise ApprovalValidationError("approval_artifact_invalid")
    return payload


def _base_report(plan: BackfillPlan, *, status: str) -> dict[str, Any]:
    return {
        "schema_version": OPERATOR_REPORT_SCHEMA_VERSION,
        "status": status,
        "lane": plan.lane,
        "phase": plan.phase,
        "vector_scope": plan.vector_scope,
        "target_fingerprint": plan.target_fingerprint,
        "selection_fingerprint": plan.selection_fingerprint,
        "profile_id": plan.profile_id,
        "model": plan.model,
        "dimension": plan.dimension,
        "receipt_run_id": plan.receipt_run_id or None,
        "receipt_source_snapshot_identity": plan.receipt_source_identity or None,
        "receipt_checkpoint_fingerprint": (
            _canonical_fingerprint({"path": plan.receipt_checkpoint_path})
            if plan.receipt_checkpoint_path
            else None
        ),
        "writer_concurrency_policy": (
            "fail_closed_approved_snapshot"
            if plan.phase == "receipts"
            else "outbox_reconcile_on_next_run"
            if plan.phase == "vectors"
            else "not_applicable"
        ),
        "limits": plan.limits,
        "time_budget": {
            "semantics": "between_batches_and_receipt_pages_only",
            "hard_wall_clock_deadline": False,
            "source_identity_scans_included": False,
            "single_batch_or_page_included": False,
        },
        "counts_available": False,
        "eligible": None,
        "selected": None,
        "attempted": None,
        "ready": None,
        "missing": None,
        "receipt": {
            "eligible": 0,
            "checked": 0,
            "ready": 0,
            "missing": 0,
            "unchecked": 0,
            "created": 0,
            "already_valid": 0,
            "conflict": 0,
            "failed": 0,
            "mutation_unknown": 0,
            "stale_skipped": 0,
            "rows_processed": 0,
            "pages_processed": 0,
        },
        "outbox": {"queued": 0, "processing": 0, "failed": 0, "dead_letter": 0},
        "groups": {"scanned": 0, "busy": 0, "quarantined": 0, "conflicted": 0},
        "elapsed_ms": 0,
        "stop_reason": "dry_run",
        "network_used": False,
        "mutation_performed": False,
        "model_calls": 0,
    }


def _emit(report: Mapping[str, Any], *, error: bool = False) -> None:
    print(json.dumps(dict(report), sort_keys=True, separators=(",", ":")), file=sys.stderr if error else sys.stdout)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge local-embedding-backfill",
        allow_abbrev=False,
        description="Plan a local embedding backfill; execute only with a matching approval artifact.",
    )
    parser.add_argument("--lane", choices=("pg", "neo4j"), required=True)
    parser.add_argument("--phase", choices=("vectors", "receipts"))
    parser.add_argument("--vector-scope", choices=("cards", "sessions", "all"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=10)
    parser.add_argument("--max-batches-per-group", type=int, default=10)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=300,
        help="budget checked between batches/pages; not a hard wall-clock deadline",
    )
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--receipt-page-size", type=int, default=64)
    parser.add_argument("--receipt-max-pages", type=int, default=10)
    parser.add_argument("--receipt-max-rows", type=int, default=500)
    parser.add_argument("--receipt-max-seconds", type=float, default=60)
    parser.add_argument("--receipt-checkpoint", default="")
    parser.add_argument("--receipt-run-id", default="")
    parser.add_argument("--receipt-source-identity", default="")
    parser.add_argument("--group-id", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        if args.lane == "pg" and args.phase is None:
            raise ValueError("pg_phase_required")
        plan = build_backfill_plan(
            lane=args.lane,
            phase=args.phase,
            vector_scope=args.vector_scope,
            target_identity=_resolve_target_identity(args.lane, env, phase=args.phase),
            group_ids=args.group_id or None,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            max_batches_per_group=args.max_batches_per_group,
            max_seconds=args.max_seconds,
            lease_seconds=args.lease_seconds,
            receipt_page_size=args.receipt_page_size,
            receipt_max_pages=args.receipt_max_pages,
            receipt_max_rows=args.receipt_max_rows,
            receipt_max_seconds=args.receipt_max_seconds,
            receipt_checkpoint_path=args.receipt_checkpoint,
            receipt_run_id=args.receipt_run_id,
            receipt_source_identity=args.receipt_source_identity,
        )
    except (TypeError, ValueError):
        reason_code = (
            "receipt_checkpoint_required"
            if args.lane == "pg"
            and args.phase == "receipts"
            and not args.receipt_checkpoint
            else "plan_limit_or_scope_invalid"
        )
        report = {
            "schema_version": OPERATOR_REPORT_SCHEMA_VERSION,
            "status": "plan_rejected",
            "reason_code": reason_code,
            "network_used": False,
            "mutation_performed": False,
            "model_calls": 0,
        }
        _emit(report, error=True)
        return 2

    if not args.execute:
        _emit(_base_report(plan, status="planned"))
        return 0
    if not args.approval:
        report = _base_report(plan, status="approval_rejected")
        report["reason_code"] = "approval_required"
        _emit(report, error=True)
        return 2
    if plan.lane == "pg" and plan.phase == "receipts" and not plan.receipt_checkpoint_path:
        report = _base_report(plan, status="approval_rejected")
        report["reason_code"] = "receipt_checkpoint_required"
        _emit(report, error=True)
        return 2
    try:
        approval = _parse_approval_file(args.approval)
        validate_approval_artifact(approval, plan=plan, command_argv=raw_argv)
    except ApprovalValidationError as exc:
        report = _base_report(plan, status="approval_rejected")
        report["reason_code"] = exc.reason_code
        _emit(report, error=True)
        return 2

    # Runtime targets and local model clients are deliberately built only after
    # the exact command and all approval bindings have passed validation.
    runtime: dict[str, Any] = {}
    close_runtime = None
    execution_started = False
    checkpoint_storage_prepared = False
    try:
        if plan.lane == "pg" and plan.phase == "receipts":
            _prepare_receipt_checkpoint_path(plan.receipt_checkpoint_path)
            checkpoint_storage_prepared = True
        execution_started = True
        runtime = _build_runtime_clients(plan, environ=env)
        close_runtime = runtime.pop("_close", None)
        if plan.lane == "pg":
            from .pg_operator_runner import run_pg_backfill_phase

            report = run_pg_backfill_phase(plan, **runtime)
        else:
            report = run_neo4j_backfill(
                plan,
                **runtime,
                group_ids=args.group_id or None,
            )
        _emit(report)
        return 0 if report.get("success") is True else 1
    except Exception as exc:
        report = _base_report(plan, status="execution_failed")
        report.update(
            {
                "counts_available": True,
                "eligible": 0,
                "selected": 0,
                "attempted": 0,
                "ready": 0,
                "missing": 0,
                "stop_reason": "execution_error",
                "network_used": execution_started,
                "mutation_performed": execution_started or checkpoint_storage_prepared,
                "reason_code": type(exc).__name__,
            }
        )
        _emit(report, error=True)
        return 1
    finally:
        if callable(close_runtime):
            try:
                close_runtime()
            except Exception:
                pass


def _counted_embedder(embedder: Any) -> _CountingLocalEmbedder:
    if isinstance(embedder, _CountingLocalEmbedder):
        return embedder
    if hasattr(embedder, "embed_document") and hasattr(embedder, "embed_documents"):
        return _CountingLocalEmbedder(embedder)

    class _Adapter:
        def __init__(self, value: Any) -> None:
            self._value = value
            self._base_url = getattr(value, "base_url", "")

        def embed_document(self, text: str):
            if hasattr(self._value, "embed"):
                return type("Result", (), {"vector": self._value.embed(text)})()
            if callable(self._value):
                return type("Result", (), {"vector": self._value(text)})()
            raise TypeError("unsupported local embedder")

        def embed_documents(self, texts: Sequence[str]):
            if hasattr(self._value, "embed_batch"):
                vectors = self._value.embed_batch(list(texts))
            elif hasattr(self._value, "embed_many"):
                vectors = self._value.embed_many(list(texts))
            else:
                return [self.embed_document(text) for text in texts]
            return [type("Result", (), {"vector": vector})() for vector in vectors]

    return _CountingLocalEmbedder(_Adapter(embedder))


def _preflight_neo4j(session: Any) -> dict[str, Any]:
    constraints = session.run(
        "SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names"
    ).single()
    indexes = session.run("SHOW INDEXES YIELD name RETURN collect(name) AS names").single()
    constraint_names = set(dict(constraints or {}).get("names") or ())
    index_names = set(dict(indexes or {}).get("names") or ())
    required_constraints = {"embedding_group_fence_group_id"}
    required_indexes = {"embedding_deletion_target_idx", "graph_op_step_receipt_idx"}
    if not required_constraints.issubset(constraint_names) or not required_indexes.issubset(index_names):
        raise RuntimeError("neo4j_safety_schema_incomplete")
    return {
        "ddl_attempted": False,
        "constraints_verified": len(required_constraints),
        "indexes_verified": len(required_indexes),
    }


def run_neo4j_backfill(
    plan: BackfillPlan,
    *,
    session: Any,
    embedder: Any,
    group_ids: Sequence[str] | None = None,
    pg_store: Any | None = None,
    journal_coordinator: Any | None = None,
) -> dict[str, Any]:
    """Freeze the target group set, execute bounded batches, and verify readback."""
    from ..llm_brain_core.local_graph_safety import GraphOperationJournalCoordinator
    from . import transition

    if pg_store is None:
        raise RuntimeError("graph_operation_journal_store_required")
    started = time.monotonic()
    schema_report = _preflight_neo4j(session)
    if group_ids is None:
        record = session.run(
            """
            MATCH (n:Entity)
            WHERE n.group_id IS NOT NULL AND n.group_id <> ''
            RETURN collect(DISTINCT n.group_id) AS groups
            """
        ).single()
        raw_groups = dict(record or {}).get("groups") or ()
    else:
        raw_groups = group_ids
    frozen_groups = _normalise_group_ids([str(value) for value in raw_groups]) or []
    if len(frozen_groups) > MAX_NEO4J_TARGET_GROUPS:
        raise ValueError("group selection exceeds the hard bound")
    group_fingerprint = _canonical_fingerprint(frozen_groups)
    counted = _counted_embedder(embedder)
    coordinator = journal_coordinator or GraphOperationJournalCoordinator(
        pg_store=pg_store,
        ensure_schema=False,
    )
    if getattr(coordinator, "_pg_store", None) is not pg_store:
        raise RuntimeError("graph_operation_journal_store_mismatch")
    if getattr(coordinator, "_ensure_schema", False):
        raise RuntimeError("neo4j_schema_creation_not_approved")

    progress = transition.backfill_neo4j_local_embeddings(
        session,
        counted,
        group_ids=frozen_groups,
        batch_size=plan.batch_size,
        max_batches_per_group=plan.max_batches_per_group,
        lease_seconds=plan.lease_seconds,
        max_seconds=plan.max_seconds,
        profile_id=plan.profile_id,
        journal_coordinator=coordinator,
        ensure_schema=False,
    )
    readback = transition.inspect_neo4j_local_embedding_state(
        session,
        group_ids=frozen_groups,
        profile_id=plan.profile_id,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    eligible = readback.eligible_entities + readback.eligible_edges
    ready = readback.ready_entities + readback.ready_edges
    missing = readback.missing_or_dirty_entities + readback.missing_or_dirty_edges
    attempted = (
        progress.entities_embedded
        + progress.edges_embedded
        + progress.entities_fenced_stale
        + progress.edges_fenced_stale
    )
    counts_consistent = (
        readback.eligible_entities == readback.ready_entities + readback.missing_or_dirty_entities
        and readback.eligible_edges == readback.ready_edges + readback.missing_or_dirty_edges
    )
    readback_verified = bool(readback.readback_verified and counts_consistent)
    groups_busy = max(progress.groups_fenced_busy, readback.groups_fenced_busy)
    groups_quarantined = max(progress.groups_quarantined, readback.groups_quarantined)
    success = (
        readback_verified
        and missing == 0
        and groups_busy == 0
        and groups_quarantined == 0
        and progress.groups_conflicted == 0
        and progress.entities_fenced_stale == 0
        and progress.edges_fenced_stale == 0
        and progress.stop_reason == "completed"
    )
    if progress.groups_conflicted:
        stop_reason = "operation_conflict"
    elif progress.groups_quarantined:
        stop_reason = "group_quarantined"
    elif progress.groups_fenced_busy:
        stop_reason = "group_busy"
    elif progress.stop_reason != "completed":
        stop_reason = progress.stop_reason
    elif not readback_verified:
        stop_reason = "readback_unverified"
    elif progress.entities_fenced_stale or progress.edges_fenced_stale:
        stop_reason = "stale_fenced"
    elif missing:
        stop_reason = "backlog_remaining"
    else:
        stop_reason = "completed"

    report = _base_report(plan, status="completed" if success else "partial")
    report.update(
        {
            "counts_available": True,
            "eligible": eligible,
            "selected": attempted,
            "attempted": attempted,
            "ready": ready,
            "missing": missing,
            "groups": {
                "scanned": len(frozen_groups),
                "busy": groups_busy,
                "quarantined": groups_quarantined,
                "conflicted": progress.groups_conflicted,
            },
            "groups_scanned": len(frozen_groups),
            "group_fingerprint": group_fingerprint,
            "schema": schema_report,
            "readback_verified": readback_verified,
            "readback_checks": {
                "complete": readback.readback_complete,
                "input_hash_mismatches": readback.readback_input_hash_mismatches,
                "profile_mismatches": readback.readback_profile_mismatches,
                "invalid_vectors": readback.readback_invalid_vectors,
                "dirty_targets": readback.readback_dirty_targets,
                "tombstoned_targets": readback.readback_tombstoned_targets,
                "tombstoned_embeddings": readback.readback_tombstoned_embeddings,
                "counts_consistent": counts_consistent,
            },
            "elapsed_ms": elapsed_ms,
            "stop_reason": stop_reason,
            "network_used": True,
            "mutation_performed": True,
            "model_calls": counted.model_calls,
            "success": success,
        }
    )
    return report


def _build_runtime_clients(
    plan: BackfillPlan,
    *,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Create runtime clients after approval; reject incomplete targets first."""
    target = _resolve_runtime_target(environ)
    if plan.lane == "pg":
        pg_dsn = target.pg_dsn
        from ..postgres_store.pgvector_store import PgVectorStore

        if plan.phase == "vectors":
            if not pg_dsn or not target.embedding_base_url:
                raise ValueError("pg_vector_targets_required")
            from ..model_connectors.policy import validate_local_embedding_write_or_query_policy

            configured_model = target.embedding_model or LOCAL_EMBEDDING_MODEL_ALIAS
            validate_local_embedding_write_or_query_policy(
                profile_id=plan.profile_id,
                model=configured_model,
                base_url=target.embedding_base_url,
                dimension=plan.dimension,
            )
            if configured_model.lower() not in {
                LOCAL_EMBEDDING_MODEL_ALIAS,
                "minishlab/potion-multilingual-128m",
                LOCAL_EMBEDDING_PROFILE_ID,
            }:
                raise ValueError("local_embedding_model_mismatch")
            store = PgVectorStore(
                dsn=pg_dsn,
                pgvector_version=str(environ.get("LLM_BRAIN_PGVECTOR_VERSION") or "0.8.0"),
                embedding_profile=plan.profile_id,
            )
            embedder = _build_local_embedder(environ, plan)
            return {"store": store, "embedder": embedder, "_close": embedder.close}

        if plan.phase == "receipts":
            if not pg_dsn or not target.couchdb_url:
                raise ValueError("pg_receipt_targets_required")
            if (target.couchdb_user or target.couchdb_password) and not (
                target.couchdb_user and target.couchdb_password
            ):
                raise ValueError("couchdb_auth_incomplete")
            store = PgVectorStore(
                dsn=pg_dsn,
                pgvector_version=str(environ.get("LLM_BRAIN_PGVECTOR_VERSION") or "0.8.0"),
                embedding_profile=plan.profile_id,
            )
            from . import transition

            source_identity, _source_count = transition._receipt_source_input_identity(
                store,
                profile_id=plan.profile_id,
                page_size=plan.receipt_page_size,
            )
            if source_identity != plan.receipt_source_identity:
                raise ValueError("receipt_source_identity_mismatch")
            from ..llm_brain_core.couchdb_projection_cli import _build_source_store

            couchdb_store = _build_source_store(
                couchdb_url=target.couchdb_url,
                couchdb_db=target.couchdb_db,
                couchdb_user=target.couchdb_user,
                couchdb_password_env="COUCHDB_PASSWORD",
                couchdb_password=target.couchdb_password,
            )

            def close_receipts() -> None:
                close = getattr(couchdb_store, "close", None)
                if callable(close):
                    close()

            return {
                "store": store,
                "couchdb_store": couchdb_store,
                "checkpoint_path": Path(plan.receipt_checkpoint_path),
                "_close": close_receipts,
            }
        raise ValueError("pg_phase_required")

    from ..model_connectors.policy import validate_local_embedding_write_or_query_policy

    embedding_url = target.embedding_base_url
    if not embedding_url:
        raise ValueError("local_embedding_target_required")
    configured_model = target.embedding_model or LOCAL_EMBEDDING_MODEL_ALIAS
    validate_local_embedding_write_or_query_policy(
        profile_id=plan.profile_id,
        model=configured_model,
        base_url=embedding_url,
        dimension=plan.dimension,
    )
    if configured_model.lower() not in {
        LOCAL_EMBEDDING_MODEL_ALIAS,
        "minishlab/potion-multilingual-128m",
        LOCAL_EMBEDDING_PROFILE_ID,
    }:
        raise ValueError("local_embedding_model_mismatch")

    neo4j_uri = target.neo4j_uri
    if not neo4j_uri or not target.pg_dsn:
        raise ValueError("neo4j_and_pg_targets_required")
    from neo4j import GraphDatabase
    from ..postgres_store.pgvector_store import PgVectorStore

    user = target.neo4j_user
    password = target.neo4j_password
    if user and not password:
        raise ValueError("neo4j_auth_incomplete")
    pg_store = PgVectorStore(
        dsn=target.pg_dsn,
        pgvector_version=str(environ.get("LLM_BRAIN_PGVECTOR_VERSION") or "0.8.0"),
        embedding_profile=plan.profile_id,
    )
    driver = GraphDatabase.driver(neo4j_uri, auth=(user, password) if user else None)
    database = target.neo4j_database
    session = driver.session(database=database) if database else driver.session()
    try:
        embedder = _build_local_embedder(environ, plan)
    except Exception:
        session.close()
        driver.close()
        raise

    def close() -> None:
        try:
            session.close()
        finally:
            driver.close()
            embedder.close()

    return {"session": session, "embedder": embedder, "pg_store": pg_store, "_close": close}


def _build_local_embedder(environ: Mapping[str, str], plan: BackfillPlan) -> _CountingLocalEmbedder:
    from .client import CanonicalLocalEmbeddingClient

    client = CanonicalLocalEmbeddingClient(
        base_url=str(environ.get("LLM_BRAIN_EMBEDDING_BASE_URL") or ""),
        api_key=str(environ.get("LLM_BRAIN_EMBEDDING_API_KEY") or "local-model2vec"),
        model=LOCAL_EMBEDDING_MODEL_ALIAS,
        profile_id=plan.profile_id,
        dimensions=plan.dimension,
        timeout_seconds=min(LOCAL_EMBEDDING_CLIENT_TIMEOUT_SECONDS, plan.max_seconds),
        max_retries=LOCAL_EMBEDDING_CLIENT_MAX_RETRIES,
    )
    return _CountingLocalEmbedder(client)


if __name__ == "__main__":
    raise SystemExit(main())
