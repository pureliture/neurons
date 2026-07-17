from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .production_authority_permission import (
    PRODUCTION_OBJECT_AUTHORITY_WRITE_CAPABILITY,
    allowed_object_class_gap,
    evaluate_production_object_authority_permission,
)


SINGLE_BOUNDED_DENIAL_ACTION = "single_bounded_denial.v1"
PERMISSION_AUDIT_STORE_EVENT_SCHEMA = "permission_audit_store_event.v2"
_HASH_REF_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS permission_denials (
    request_hash TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'permission_audit_store_event.v2'),
    actor_ref_hash TEXT NOT NULL,
    append_attempt_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action = 'single_bounded_denial.v1'),
    permission TEXT NOT NULL CHECK (permission = 'denied'),
    authority_write_performed INTEGER NOT NULL CHECK (authority_write_performed = 0),
    production_mutation_performed INTEGER NOT NULL CHECK (production_mutation_performed = 0),
    appended_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (actor_ref_hash)
)
"""
_CANONICAL_DECISION_KEYS = frozenset(
    {
        "allowed",
        "gate_provided",
        "missing_gate_evidence",
        "approval_ref_hash",
        "project",
        "target_object_id",
    }
)
_REQUIRED_AUDIT_DENIAL_GAPS = frozenset(
    {
        "approved",
        "approval_ref",
        "single_project_single_object_scope",
        "project_scope_match",
        "max_objects_1",
        "configured_deployed_mcp_identity_matches_source",
        "read_after_write_smoke_plan",
        "rollback_or_supersession_plan",
        "no_raw_private_evidence",
        allowed_object_class_gap(),
    }
)


class PermissionAuditStoreError(RuntimeError):
    """Raised when the isolated permission-audit store cannot prove its write."""


class PermissionAuditStore:
    """Append/read-back store isolated from product authority and corpus state."""

    def __init__(
        self,
        path: str | Path,
        *,
        denial_policy_evaluator: Callable[..., Mapping[str, Any]] = (
            evaluate_production_object_authority_permission
        ),
    ) -> None:
        if not callable(denial_policy_evaluator):
            raise ValueError("permission-audit denial policy evaluator is required")
        self._path = Path(path)
        self._denial_policy_evaluator = denial_policy_evaluator
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path, timeout=30) as connection:
            connection.execute(_STORE_SCHEMA)
            columns = connection.execute("PRAGMA table_info(permission_denials)").fetchall()
            schema_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'permission_denials'"
            ).fetchone()
        expected_columns = [
            "request_hash",
            "schema_version",
            "actor_ref_hash",
            "append_attempt_hash",
            "action",
            "permission",
            "authority_write_performed",
            "production_mutation_performed",
            "appended_at",
        ]
        schema_sql = " ".join(str(schema_row[0] if schema_row else "").lower().split())
        required_constraints = (
            "check (schema_version = 'permission_audit_store_event.v2')",
            "check (action = 'single_bounded_denial.v1')",
            "check (permission = 'denied')",
            "check (authority_write_performed = 0)",
            "check (production_mutation_performed = 0)",
            "unique (actor_ref_hash)",
        )
        if (
            [str(row[1]) for row in columns] != expected_columns
            or int(columns[0][5] if columns else 0) != 1
            or any(constraint not in schema_sql for constraint in required_constraints)
        ):
            raise PermissionAuditStoreError("permission-audit store schema mismatch")

    def append_denied_once(
        self,
        *,
        request_hash: str,
        actor_ref_hash: str,
        action: str,
        append_attempt_hash: str,
    ) -> dict[str, object]:
        _require_hash_ref(request_hash, field="request_hash")
        _require_hash_ref(actor_ref_hash, field="actor_ref_hash")
        _require_hash_ref(append_attempt_hash, field="append_attempt_hash")
        if action != SINGLE_BOUNDED_DENIAL_ACTION:
            raise ValueError("unsupported permission-audit action")

        with sqlite3.connect(self._path, timeout=30, isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT request_hash, actor_ref_hash
                    FROM permission_denials
                    WHERE request_hash = ? OR actor_ref_hash = ?
                    """,
                    (request_hash, actor_ref_hash),
                ).fetchall()
                if len(existing) > 1:
                    raise PermissionAuditStoreError(
                        "permission-audit single-winner invariant failed"
                    )
                if existing:
                    existing_request_hash, existing_actor_ref_hash = existing[0]
                    if (
                        existing_request_hash != request_hash
                        or existing_actor_ref_hash != actor_ref_hash
                    ):
                        raise PermissionAuditStoreError(
                            "permission-audit bound actor already consumed"
                        )
                    appended = 0
                else:
                    decision = self._denial_policy_evaluator(
                        {},
                        capability=PRODUCTION_OBJECT_AUTHORITY_WRITE_CAPABILITY,
                        action=action,
                        # Prove that the human gate itself denies the request even
                        # under the maximally permissive product capability state.
                        # This sidecar has no product write client or credential.
                        service_write_enabled=True,
                        ledger_read_only=False,
                    )
                    _validate_canonical_denial_decision(decision)
                    cursor = connection.execute(
                        """
                        INSERT INTO permission_denials (
                            request_hash,
                            schema_version,
                            actor_ref_hash,
                            append_attempt_hash,
                            action,
                            permission,
                            authority_write_performed,
                            production_mutation_performed
                        ) VALUES (?, 'permission_audit_store_event.v2', ?, ?, ?, 'denied', 0, 0)
                        """,
                        (request_hash, actor_ref_hash, append_attempt_hash, action),
                    )
                    appended = cursor.rowcount
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        readback = self.readback(request_hash=request_hash)
        if (
            readback.get("status") != "recorded"
            or readback.get("actor_ref_hash") != actor_ref_hash
            or readback.get("action") != action
            or readback.get("permission") != "denied"
            or readback.get("authority_write_performed") is not False
            or readback.get("production_mutation_performed") is not False
        ):
            raise PermissionAuditStoreError("permission-audit read-after-write mismatch")

        return {
            "status": "recorded",
            "append_count": appended,
            "stored_row_count": readback["stored_row_count"],
            "read_after_write_status": "validated",
            "request_hash": request_hash,
            "append_attempt_hash": readback["append_attempt_hash"],
            "schema_version": "bounded_permission_denial_result.v1",
            "action": readback["action"],
            "ledger_scope": "production",
            "permission": readback["permission"],
            "authority_write_performed": readback["authority_write_performed"],
            "production_mutation_performed": False,
            "actor_ref_hash": readback["actor_ref_hash"],
            "protected_values_returned": False,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        }

    def readback(self, *, request_hash: str) -> dict[str, object]:
        _require_hash_ref(request_hash, field="request_hash")
        with sqlite3.connect(self._path, timeout=30) as connection:
            rows = connection.execute(
                """
                SELECT schema_version, actor_ref_hash, append_attempt_hash, action, permission,
                       authority_write_performed, production_mutation_performed
                FROM permission_denials
                WHERE request_hash = ?
                """,
                (request_hash,),
            ).fetchall()
        if len(rows) != 1:
            return {
                "schema_version": "permission_audit_store_readback.v2",
                "status": "missing",
                "stored_row_count": len(rows),
                "request_hash": request_hash,
                "production_mutation_performed": False,
            }
        (
            schema_version,
            actor_ref_hash,
            append_attempt_hash,
            action,
            permission,
            authority_write,
            mutation,
        ) = rows[0]
        if (
            schema_version != PERMISSION_AUDIT_STORE_EVENT_SCHEMA
            or not isinstance(actor_ref_hash, str)
            or _HASH_REF_PATTERN.fullmatch(actor_ref_hash) is None
            or not isinstance(append_attempt_hash, str)
            or _HASH_REF_PATTERN.fullmatch(append_attempt_hash) is None
            or action != SINGLE_BOUNDED_DENIAL_ACTION
            or permission != "denied"
            or type(authority_write) is not int
            or authority_write != 0
            or type(mutation) is not int
            or mutation != 0
        ):
            raise PermissionAuditStoreError("permission-audit read-after-write mismatch")
        return {
            "schema_version": "permission_audit_store_readback.v2",
            "status": "recorded",
            "stored_row_count": 1,
            "request_hash": request_hash,
            "append_attempt_hash": append_attempt_hash,
            "actor_ref_hash": actor_ref_hash,
            "action": action,
            "permission": permission,
            "authority_write_performed": False,
            "production_mutation_performed": False,
        }


def _validate_canonical_denial_decision(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _CANONICAL_DECISION_KEYS:
        raise PermissionAuditStoreError("permission-audit denial policy mismatch")
    missing = value.get("missing_gate_evidence")
    if (
        value.get("allowed") is not False
        or value.get("gate_provided") is not False
        or not isinstance(missing, list)
        or not _REQUIRED_AUDIT_DENIAL_GAPS.issubset(set(missing))
        or "service_production_object_authority_write_flag" in missing
        or "writable_ledger" in missing
        or value.get("approval_ref_hash") != ""
        or value.get("project") != ""
        or value.get("target_object_id") != ""
    ):
        raise PermissionAuditStoreError("permission-audit denial policy mismatch")


def _require_hash_ref(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _HASH_REF_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sha256 hash reference")


class PermissionAuditStoreHttpServer(ThreadingHTTPServer):
    store: PermissionAuditStore


def build_permission_audit_store_server(
    *,
    database: str | Path,
    host: str = "127.0.0.1",
    port: int = 8771,
) -> PermissionAuditStoreHttpServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("permission-audit store must bind loopback")
    server = PermissionAuditStoreHttpServer((host, port), _PermissionAuditStoreHandler)
    server.store = PermissionAuditStore(database)
    return server


class _PermissionAuditStoreHandler(BaseHTTPRequestHandler):
    server: PermissionAuditStoreHttpServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            payload = self._read_json()
            if self.path == "/append-denied-once":
                if set(payload) != {
                    "request_hash",
                    "actor_ref_hash",
                    "action",
                    "append_attempt_hash",
                }:
                    raise ValueError("invalid append request")
                result = self.server.store.append_denied_once(
                    request_hash=payload.get("request_hash"),
                    actor_ref_hash=payload.get("actor_ref_hash"),
                    action=payload.get("action"),
                    append_attempt_hash=payload.get("append_attempt_hash"),
                )
            elif self.path == "/readback":
                if set(payload) != {"request_hash"}:
                    raise ValueError("invalid readback request")
                result = self.server.store.readback(
                    request_hash=payload.get("request_hash"),
                )
            else:
                self._write_json(404, {"status": "not_found"})
                return
        except (TypeError, ValueError, PermissionAuditStoreError, json.JSONDecodeError):
            self._write_json(400, {"status": "error"})
            return
        except Exception:
            self._write_json(500, {"status": "error"})
            return
        self._write_json(200, result)

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get("Content-Type") != "application/json":
            raise ValueError("invalid content type")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 2 or length > 4096:
            raise ValueError("invalid content length")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("invalid json object")
        return value

    def _write_json(self, status: int, value: Mapping[str, object] | dict[str, object]) -> None:
        body = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="permission-audit-store")
    parser.add_argument("--database", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args(argv)
    server = build_permission_audit_store_server(
        database=args.database,
        host=args.host,
        port=args.port,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
