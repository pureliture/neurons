from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..couchdb_source.document_model import projection_state_doc_id
from ..model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from ..rag_ingress.qdrant_couchdb_authority import (
    CouchDBProjectionStateAuthorityResolver,
)


COMMAND = "session-memory-stale-plan"
SCHEMA_VERSION = "session_memory_stale_plan.v1"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_LIMIT_SESSIONS = 20_000
DEFAULT_BATCH_SESSIONS = 100
CLASSIFICATIONS = (
    "current",
    "stale",
    "unresolved",
    "stale_past_retention",
)
REPRESENTATIONS = ("v3", "v4", "unknown")
FAILURE_REASONS = (
    "projection_state_missing",
    "receipt_missing",
    "profile_mismatch",
    "receipt_invalid",
    "current_row_missing",
    "authority_rejected",
    "authority_error",
)


def _empty_counts() -> dict[str, int]:
    return {"rows": 0, "bytes": 0}


def _parse_observed_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _receipt_from_state(
    state_document: dict[str, Any] | None,
    *,
    embedding_profile: str,
) -> tuple[dict[str, Any] | None, str, str]:
    if state_document is None:
        return None, "unknown", "projection_state_missing"
    if not isinstance(state_document, dict):
        return None, "unknown", "receipt_invalid"
    receipts = state_document.get("backend_receipts")
    if not isinstance(receipts, dict):
        return None, "unknown", "receipt_missing"
    local_receipts = receipts.get("postgres_pgvector_local")
    if not isinstance(local_receipts, dict):
        return None, "unknown", "receipt_missing"
    receipt = local_receipts.get(embedding_profile)
    if not isinstance(receipt, dict):
        reason = "profile_mismatch" if local_receipts else "receipt_missing"
        return None, "unknown", reason
    version = receipt.get("receipt_version")
    if type(version) is not int or version not in (3, 4):
        return None, "unknown", "receipt_invalid"
    representation = f"v{version}"
    if version == 3:
        receipt_ids = receipt.get("session_memory_knowledge_id")
        if not isinstance(receipt_ids, str) or not receipt_ids:
            return None, representation, "receipt_invalid"
    else:
        receipt_ids = receipt.get("segment_ids")
        if (
            not isinstance(receipt_ids, list)
            or not receipt_ids
            or any(not isinstance(value, str) or not value for value in receipt_ids)
        ):
            return None, representation, "receipt_invalid"
    return receipt, representation, ""


def _receipt_ids(receipt: dict[str, Any]) -> tuple[str, ...]:
    if receipt.get("receipt_version") == 3:
        return (str(receipt["session_memory_knowledge_id"]),)
    return tuple(str(value) for value in receipt["segment_ids"])


class SessionMemoryStalePlanner:
    def __init__(
        self,
        *,
        source_store: Any,
        pg_store: Any,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        limit_sessions: int = DEFAULT_LIMIT_SESSIONS,
        batch_sessions: int = DEFAULT_BATCH_SESSIONS,
        now_fn: Callable[[], datetime] | None = None,
        authority_resolver: Any | None = None,
        embedding_profile: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> None:
        if isinstance(retention_days, bool) or retention_days < 0:
            raise ValueError("retention_days must not be negative")
        if isinstance(limit_sessions, bool) or limit_sessions < 1:
            raise ValueError("limit_sessions must be positive")
        if isinstance(batch_sessions, bool) or batch_sessions < 1:
            raise ValueError("batch_sessions must be positive")
        self._source_store = source_store
        self._pg_store = pg_store
        self._retention_days = int(retention_days)
        self._limit_sessions = int(limit_sessions)
        self._batch_sessions = int(batch_sessions)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._embedding_profile = embedding_profile
        self._authority_resolver = (
            authority_resolver
            if authority_resolver is not None
            else CouchDBProjectionStateAuthorityResolver(
                source_store,
                backend="postgres_pgvector_local",
                embedding_profile=embedding_profile,
                pg_store=pg_store,
            )
        )

    def run(self) -> dict[str, Any]:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        retention_cutoff = now.astimezone(timezone.utc) - timedelta(
            days=self._retention_days
        )
        counts = {name: _empty_counts() for name in CLASSIFICATIONS}
        by_representation = {
            representation: {
                name: _empty_counts() for name in CLASSIFICATIONS
            }
            for representation in REPRESENTATIONS
        }
        failure_reasons = {
            reason: {"sessions": 0, "rows": 0}
            for reason in FAILURE_REASONS
        }
        stale_sessions: set[str] = set()
        projects: set[str] = set()
        sessions_scanned = 0

        for session_id_hash, rows in self._pg_store.iter_session_chunk_batches(
            batch_size=self._batch_sessions,
            limit_sessions=self._limit_sessions,
        ):
            sessions_scanned += 1
            if not rows:
                continue
            for row in rows:
                if row.project:
                    projects.add(row.project)
            state_document = None
            read_failed = False
            try:
                state_document = self._source_store.get(
                    projection_state_doc_id(session_id_hash)
                )
            except Exception:
                read_failed = True
            receipt, representation, receipt_reason = _receipt_from_state(
                state_document,
                embedding_profile=self._embedding_profile,
            )
            reason = "authority_error" if read_failed else receipt_reason
            authorized_ids: set[str] = set()
            if receipt is not None and not read_failed:
                receipt_ids = _receipt_ids(receipt)
                row_by_id = {row.chunk_id: row for row in rows}
                authority_row = row_by_id.get(receipt_ids[0])
                if authority_row is None:
                    reason = "current_row_missing"
                else:
                    hit = {
                        "session_id_hash": authority_row.session_id_hash,
                        "memory_id": authority_row.chunk_id,
                        "content_hash": authority_row.content_hash,
                        "provider": authority_row.provider,
                        "project": authority_row.project,
                    }
                    try:
                        authorized = self._authority_resolver.resolve(hit)
                    except Exception:
                        authorized = None
                        reason = "authority_error"
                    if authorized is None:
                        if reason != "authority_error":
                            reason = "authority_rejected"
                    else:
                        if receipt.get("receipt_version") == 4:
                            resolved_ids = authorized.get("segment_ids")
                            if (
                                authorized.get("representation") != "segment_v1"
                                or not isinstance(resolved_ids, list)
                                or any(not isinstance(value, str) for value in resolved_ids)
                                or authority_row.chunk_id not in resolved_ids
                            ):
                                reason = "authority_rejected"
                            else:
                                authorized_ids = set(resolved_ids)
                                reason = ""
                        elif authorized.get("representation") == "segment_v1":
                            reason = "authority_rejected"
                        else:
                            authorized_ids = {authority_row.chunk_id}
                            reason = ""

            if not authorized_ids:
                failure_reasons[reason]["sessions"] += 1
                failure_reasons[reason]["rows"] += len(rows)
            for row in rows:
                row_classification = (
                    "unresolved"
                    if not authorized_ids
                    else "current"
                    if row.chunk_id in authorized_ids
                    else "stale"
                )
                row_bytes = len(row.content_markdown.encode("utf-8"))
                counts[row_classification]["rows"] += 1
                counts[row_classification]["bytes"] += row_bytes
                by_representation[representation][row_classification]["rows"] += 1
                by_representation[representation][row_classification]["bytes"] += row_bytes
                if row_classification == "stale":
                    stale_sessions.add(session_id_hash)
                    observed_at = _parse_observed_at(row.observed_at)
                    if observed_at is not None and observed_at < retention_cutoff:
                        counts["stale_past_retention"]["rows"] += 1
                        counts["stale_past_retention"]["bytes"] += row_bytes
                        by_representation[representation]["stale_past_retention"][
                            "rows"
                        ] += 1
                        by_representation[representation]["stale_past_retention"][
                            "bytes"
                        ] += row_bytes

        return {
            "schema_version": SCHEMA_VERSION,
            "command": COMMAND,
            "mode": "dry_run",
            "retention_days": self._retention_days,
            "limit_sessions": self._limit_sessions,
            "batch_sessions": self._batch_sessions,
            "sessions_scanned": sessions_scanned,
            "classifications": counts,
            "by_representation": by_representation,
            "stale_sessions": len(stale_sessions),
            "project_count": len(projects),
            "failure_reasons": failure_reasons,
            "mutation_performed": False,
            "raw_ids_printed": False,
            "project_names_printed": False,
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument(
        "--limit-sessions",
        type=int,
        default=DEFAULT_LIMIT_SESSIONS,
    )
    parser.add_argument(
        "--batch-sessions",
        type=int,
        default=DEFAULT_BATCH_SESSIONS,
    )
    return parser


def _build_source_store(environ: Mapping[str, str]) -> Any:
    from ..couchdb_source.couchdb_http_store import (
        CouchDBHttpSourceStore,
        basic_auth_header,
    )

    base_url = str(environ.get("COUCHDB_URL") or "").strip()
    if not base_url:
        raise ValueError("COUCHDB_URL is required")
    user = str(environ.get("COUCHDB_USER") or "")
    password = str(environ.get("COUCHDB_PASSWORD") or "")
    return CouchDBHttpSourceStore(
        base_url=base_url,
        db=str(environ.get("COUCHDB_DB") or "transcript_source"),
        auth_header=basic_auth_header(user=user, password=password) if user else "",
    )


def _build_pg_store(environ: Mapping[str, str]) -> Any:
    from ..postgres_store.pgvector_store import PgVectorStore

    dsn = (
        environ.get("NEURON_LBRAIN_PGVECTOR_DSN", "")
        or environ.get("LLM_BRAIN_PGVECTOR_DSN", "")
        or environ.get("NEURON_LEDGER_PG_DSN", "")
    )
    if not dsn:
        raise ValueError("PostgreSQL DSN is required")
    return PgVectorStore(
        dsn=dsn,
        pgvector_version=environ.get("LLM_BRAIN_PGVECTOR_VERSION", "0.8.0"),
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    source_store: Any | None = None,
    pg_store: Any | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    env = environ if environ is not None else os.environ
    owns_source_store = source_store is None
    owns_pg_store = pg_store is None
    try:
        current_source_store = (
            source_store if source_store is not None else _build_source_store(env)
        )
        current_pg_store = pg_store if pg_store is not None else _build_pg_store(env)
        report = SessionMemoryStalePlanner(
            source_store=current_source_store,
            pg_store=current_pg_store,
            retention_days=args.retention_days,
            limit_sessions=args.limit_sessions,
            batch_sessions=args.batch_sessions,
        ).run()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("session_memory_stale_plan_failed", file=sys.stderr)
        return 1
    finally:
        if owns_pg_store and pg_store is None:
            closer = getattr(locals().get("current_pg_store"), "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        if owns_source_store and source_store is None:
            closer = getattr(locals().get("current_source_store"), "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
