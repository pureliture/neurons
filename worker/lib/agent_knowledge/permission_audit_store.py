from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


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
    appended_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


class PermissionAuditStoreError(RuntimeError):
    """Raised when the isolated permission-audit store cannot prove its write."""


class PermissionAuditStore:
    """Append/read-back store isolated from product authority and corpus state."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
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
                    ON CONFLICT(request_hash) DO NOTHING
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
            "production_mutation_performed": False,
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
