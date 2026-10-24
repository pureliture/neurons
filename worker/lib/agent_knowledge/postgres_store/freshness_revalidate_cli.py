"""Deterministic freshness revalidation for LBrain memory cards.

The resolver derives `freshness` at read time from the stored verification
history (`freshness_checked_at` / `freshness_next_check_after` / `source_hash`).
Nothing wrote that history, so every card stayed `unchecked`. This command
establishes and refreshes it from a deterministic source only: the card's
`source_ref[0].source_id` must resolve to a live ``session_memory_chunks`` row,
and the chunk's content hash is compared with the baseline recorded on the card.

No source, no claim: cards without a resolvable source are reported and left
`unchecked` (FR-AGE-041 — never present an unverified card as verified).

Default invocation plans only. ``--check`` reads the database. ``--execute``
writes the four freshness columns for resolvable cards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.conninfo import conninfo_to_dict

from .pgvector_store import PgVectorStore

_REPORT_SCHEMA = "lbrain_freshness_revalidate.v1"
_DSN_ENV_NAMES = (
    "NEURON_LBRAIN_PGVECTOR_DSN",
    "LLM_BRAIN_PGVECTOR_DSN",
    "NEURON_LEDGER_PG_DSN",
)
_DEFAULT_INTERVAL_DAYS = 30

_RESOLVABLE_SQL = """
SELECT c.memory_id,
       c.source_hash,
       c.freshness_state,
       s.chunk_id,
       s.content_hash,
       s.content_markdown
  FROM memory_cards c
  JOIN session_memory_chunks s
    ON s.chunk_id = c.source_ref->0->>'source_id'
 WHERE c.authorization_status = 'active'
 ORDER BY c.memory_id
 LIMIT %s OFFSET %s
"""

_UNRESOLVED_SQL = """
SELECT count(*)
  FROM memory_cards c
 WHERE c.authorization_status = 'active'
   AND NOT EXISTS (
        SELECT 1 FROM session_memory_chunks s
         WHERE s.chunk_id = c.source_ref->0->>'source_id')
"""

_UPDATE_SQL = """
UPDATE memory_cards
   SET freshness_state = %s,
       freshness_checked_at = %s,
       freshness_next_check_after = %s,
       source_hash = %s
 WHERE memory_id = %s
"""


def _emit(report: dict[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


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


def content_sha256(markdown: str | None) -> str:
    """Hash exactly the bytes the canonical chunk store hashes."""

    return "sha256:" + hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()


def classify(baseline: str | None, current: str) -> tuple[str, str]:
    """Return (freshness_state, source_hash_to_store).

    A missing baseline means this verification establishes the baseline, so the
    card is current as of now. A different baseline means the source changed
    since it was recorded, and the baseline is kept so the mismatch persists
    until a human supersedes or re-approves the card (FR-AGE-045).
    """

    if not baseline:
        return "verified_current", current
    if baseline == current:
        return "verified_current", baseline
    return "verified_changed", baseline


def plan(
    store: PgVectorStore,
    *,
    interval_days: int,
    limit: int,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    next_check = now + timedelta(days=interval_days)
    counts: dict[str, int] = {}
    changes: list[dict[str, str]] = []
    entries: list[tuple[str, str, str, str | None, str]] = []

    with store.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(_RESOLVABLE_SQL, (limit, 0))
        rows = cursor.fetchall()
        names = [column.name for column in cursor.description]
        unresolved_row = conn.execute(_UNRESOLVED_SQL).fetchone()
        unresolved = (
            list(unresolved_row.values())[0]
            if isinstance(unresolved_row, Mapping)
            else unresolved_row[0]
        )

    def value(row: Any, name: str, index: int) -> Any:
        if isinstance(row, Mapping):
            return row.get(name)
        return row[index]

    for row in rows:
        memory_id = str(value(row, "memory_id", 0))
        baseline = value(row, "source_hash", 1)
        current = content_sha256(value(row, "content_markdown", 5))
        state, stored_hash = classify(baseline, current)
        counts[state] = counts.get(state, 0) + 1
        if state == "verified_changed" and len(changes) < 5:
            changes.append({"memory_id": memory_id, "chunk_id": str(value(row, "chunk_id", 3))})
        entries.append((memory_id, state, stored_hash, now.isoformat(), next_check.isoformat()))

    return {
        "schema_version": _REPORT_SCHEMA,
        "interval_days": interval_days,
        "checked_at": now.isoformat(),
        "resolvable_cards": len(rows),
        "unresolved_active_cards": int(unresolved),
        "counts": counts,
        "verified_changed_sample": changes,
        "entries": entries,
        "column_names": names,
    }


def apply(store: PgVectorStore, entries: list[tuple[str, str, str, str, str]]) -> int:
    with store.transaction() as conn:
        for memory_id, state, source_hash, checked_at, next_check in entries:
            conn.execute(
                _UPDATE_SQL,
                (state, checked_at, next_check, source_hash, memory_id),
            )
    return len(entries)


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge pg-freshness-revalidate", allow_abbrev=False)
    parser.add_argument("--check", action="store_true", help="read-only report")
    parser.add_argument("--execute", action="store_true", help="write freshness history")
    parser.add_argument("--interval-days", type=int, default=_DEFAULT_INTERVAL_DAYS)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args(argv)
    if args.interval_days < 1 or args.interval_days > 3650:
        parser.error("--interval-days must be between 1 and 3650")
    if args.limit < 1 or args.limit > 100000:
        parser.error("--limit must be between 1 and 100000")
    if args.check and args.execute:
        parser.error("choose either --check or --execute")

    env = environ if environ is not None else os.environ
    dsn_env, dsn = _resolve_dsn(env)
    report: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA,
        "status": "planned",
        "dsn_env": dsn_env or None,
        "mutation_performed": False,
        "network_used": False,
    }
    if not args.check and not args.execute:
        _emit(report)
        return 0
    if not dsn:
        report.update(status="configuration_required", reason="postgres_dsn_not_configured")
        _emit(report)
        return 2
    if not _explicit_target(dsn):
        report.update(status="configuration_rejected", reason="explicit_database_and_user_required")
        _emit(report)
        return 2

    store = PgVectorStore(dsn=dsn)
    report["network_used"] = True
    try:
        result = plan(store, interval_days=args.interval_days, limit=args.limit)
        report.update({k: v for k, v in result.items() if k != "entries"})
        report["entries"] = len(result["entries"])
        if args.execute:
            written = apply(store, result["entries"])
            report.update(status="applied", mutation_performed=True, written=written)
        else:
            report["status"] = "checked"
        _emit(report)
        return 0
    except Exception as exc:  # never echo driver text: it may carry the DSN
        report.update(status="failed", error_type=type(exc).__name__)
        _emit(report)
        return 1


if __name__ == "__main__":
    sys.exit(main())
