"""Approval-gated, additive application of the LBrain PostgreSQL schema.

Default invocation is planning-only: it parses the checked-in DDL and never
opens a database connection. ``--check`` is read-only. ``--execute`` applies the
whole idempotent schema, and production execution additionally requires an
explicit approval bound to the exact schema hash plus backup/rollback refs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping
from typing import Any

from psycopg.conninfo import conninfo_to_dict

from ..public_safe_util import require_sha256
from .pgvector_store import PgVectorStore

_SCHEMA_VERSION = "lbrain_pg_schema_apply.v1"
_SCHEMA_PATH = "pgvector_schema.sql"
_DSN_ENV_NAMES = (
    "NEURON_LBRAIN_PGVECTOR_DSN",
    "LLM_BRAIN_PGVECTOR_DSN",
    "NEURON_LEDGER_PG_DSN",
)
_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_INDEX_RE = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_EXTENSION_RE = re.compile(
    r'\bCREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+"?([a-zA-Z0-9_-]+)"?',
    re.IGNORECASE,
)
_COLUMN_RE = re.compile(
    r"\bALTER\s+TABLE\s+([a-z_][a-z0-9_]*)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def schema_manifest(sql: str) -> dict[str, list[str]]:
    """Return the objects the checked-in schema declares as idempotent DDL."""

    return {
        "tables": sorted(set(_TABLE_RE.findall(sql))),
        "indexes": sorted(set(_INDEX_RE.findall(sql))),
        "extensions": sorted(set(_EXTENSION_RE.findall(sql))),
        "columns": sorted({f"{table}.{column}" for table, column in _COLUMN_RE.findall(sql)}),
    }


def schema_sha256(sql: str) -> str:
    """Bind an approval to the exact DDL bytes that will be applied."""

    return "sha256:" + hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _resolve_dsn(environ: Mapping[str, str]) -> tuple[str, str]:
    for name in _DSN_ENV_NAMES:
        value = str(environ.get(name) or "").strip()
        if value:
            return name, value
    return "", ""


def _explicit_target(dsn: str) -> bool:
    try:
        config = conninfo_to_dict(dsn)
    except Exception:
        return False
    return bool(config.get("dbname") and config.get("user")) and not config.get("service")


def _local_dsn(dsn: str) -> bool:
    try:
        config = conninfo_to_dict(dsn)
    except Exception:
        return False
    host = str(config.get("host") or "")
    if config.get("hostaddr") or config.get("service"):
        return False
    if not host:
        return True
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")


def _valid_ref(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and len(text) <= 256 and "\n" not in text and "\r" not in text


def _present(row: Any) -> bool:
    if isinstance(row, Mapping):
        return bool(row.get("present"))
    return bool(row[0]) if row is not None else False


def _missing_objects(store: PgVectorStore, manifest: Mapping[str, list[str]]) -> list[str]:
    missing: list[str] = []
    with store.transaction() as conn:
        with conn.cursor() as cur:
            for table in manifest["tables"]:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (table,))
                if not _present(cur.fetchone()):
                    missing.append(f"table:{table}")
            for index in manifest["indexes"]:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (index,))
                if not _present(cur.fetchone()):
                    missing.append(f"index:{index}")
            for extension in manifest["extensions"]:
                cur.execute(
                    "SELECT n.nspname AS schema_name FROM pg_extension e "
                    "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = %s",
                    (extension,),
                )
                row = cur.fetchone()
                if row is None:
                    missing.append(f"extension:{extension}")
                    continue
                schema_name = row.get("schema_name") if isinstance(row, Mapping) else row[0]
                if schema_name != "public":
                    missing.append(f"extension_schema:{extension}")
            for item in manifest["columns"]:
                table, column = item.split(".", 1)
                cur.execute(
                    "SELECT 1 AS present FROM information_schema.columns "
                    "WHERE table_schema = ANY(current_schemas(false)) "
                    "AND table_name = %s AND column_name = %s",
                    (table, column),
                )
                if cur.fetchone() is None:
                    missing.append(f"column:{item}")
    return sorted(missing)


def _emit(report: dict[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge pg-schema-apply")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only schema object verification")
    mode.add_argument("--execute", action="store_true", help="Apply the additive schema")
    parser.add_argument("--target", choices=("local_test", "production"), default="local_test")
    parser.add_argument("--approved", action="store_true", help="Required for production execution")
    parser.add_argument("--approval-ref", default="", help="sha256 binding approval to the schema hash")
    parser.add_argument("--backup-ref", default="", help="Required production backup reference")
    parser.add_argument("--rollback-ref", default="", help="Required production rollback reference")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ

    sql = PgVectorStore.schema_sql()
    manifest = schema_manifest(sql)
    schema_hash = schema_sha256(sql)
    dsn_env, dsn = _resolve_dsn(env)
    report: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "status": "planned",
        "target": args.target,
        "schema_path": _SCHEMA_PATH,
        "schema_sha256": schema_hash,
        "tables": manifest["tables"],
        "indexes": manifest["indexes"],
        "extensions": manifest["extensions"],
        "columns": manifest["columns"],
        "missing": [],
        "mutation_performed": False,
        "production_mutation_performed": False,
        "network_used": False,
        "dsn_env": dsn_env or None,
        "approval_ref_present": bool(args.approval_ref),
        "backup_ref_present": bool(args.backup_ref),
        "rollback_ref_present": bool(args.rollback_ref),
    }

    if not args.check and not args.execute:
        _emit(report)
        return 0

    if args.execute and args.target == "production":
        missing: list[str] = []
        if not args.approved:
            missing.append("approved")
        try:
            approval_ref = require_sha256(args.approval_ref, "approval_ref")
        except ValueError:
            approval_ref = ""
            missing.append("approval_ref_sha256")
        if approval_ref and approval_ref != schema_hash:
            missing.append("approval_ref_matches_schema")
        if not _valid_ref(args.backup_ref):
            missing.append("backup_ref")
        if not _valid_ref(args.rollback_ref):
            missing.append("rollback_ref")
        if missing:
            report.update(
                status="denied",
                reason="production_schema_apply_requires_approval",
                missing=sorted(missing),
            )
            _emit(report)
            return 1

    if not dsn:
        report.update(status="configuration_required", reason="postgres_dsn_not_configured")
        _emit(report)
        return 2

    if not _explicit_target(dsn):
        report.update(status="configuration_rejected", reason="explicit_database_and_user_required")
        _emit(report)
        return 2

    if args.execute and args.target == "local_test" and not _local_dsn(dsn):
        report.update(status="denied", reason="local_test_target_requires_local_dsn")
        _emit(report)
        return 1

    store = PgVectorStore(dsn=dsn)
    try:
        if args.execute:
            store.execute_ddl(sql)
            report["mutation_performed"] = True
            report["production_mutation_performed"] = args.target == "production"
        report["network_used"] = True
        missing_objects = _missing_objects(store, manifest)
        report["missing"] = missing_objects
        if args.execute:
            report["status"] = "applied" if not missing_objects else "verification_failed"
        else:
            report["status"] = "ok" if not missing_objects else "missing"
        _emit(report)
        return 0 if not missing_objects else 1
    except Exception as exc:
        # Never echo exception text: a driver error may contain a DSN or host.
        report.update(status="failed", error_type=type(exc).__name__)
        _emit(report)
        return 1
