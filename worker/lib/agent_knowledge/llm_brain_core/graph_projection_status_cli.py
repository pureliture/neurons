from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source.document_model import SourceDocType
from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore
from agent_knowledge.ledger import Ledger
from agent_knowledge.ledger_base import _table_exists

from .couchdb_projection_cli import _build_source_store, _filter_sessions, _project_ref
from .ledger_adapter import EXTRACTION_LEVEL_ENTITY, EXTRACTION_LEVEL_EPISODIC

GRAPH_PROJECTION_STATUS_SCHEMA_VERSION = "llm_brain_graph_projection_status.v1"
SOURCE_INVALID_UPSERT_RESULTS = frozenset({"source_invalid", "invalid_source"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-graph-status")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--couchdb-url", default=os.environ.get("COUCHDB_URL", ""))
    parser.add_argument("--couchdb-db", default=os.environ.get("COUCHDB_DB", "transcript_source"))
    parser.add_argument("--couchdb-user", default=os.environ.get("COUCHDB_USER", ""))
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    parser.add_argument("--progress-jsonl", action="append", default=[])
    parser.add_argument("--dead-letter-jsonl", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        source_store = _build_source_store(
            couchdb_url=args.couchdb_url,
            couchdb_db=args.couchdb_db,
            couchdb_user=args.couchdb_user,
            couchdb_password_env=args.couchdb_password_env,
        )
        report = build_graph_projection_status(
            ledger_path=Path(args.ledger),
            source_store=source_store,
            project=str(args.project or ""),
            provider=str(args.provider or ""),
            progress_jsonl=[Path(item) for item in args.progress_jsonl or []],
            dead_letter_jsonl=[Path(item) for item in args.dead_letter_jsonl or []],
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": GRAPH_PROJECTION_STATUS_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "message": "graph projection status failed",
                    "raw_paths_printed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0


def build_graph_projection_status(
    *,
    ledger_path: Path,
    source_store: CouchDBSourceStore,
    project: str = "",
    provider: str = "",
    progress_jsonl: list[Path] | None = None,
    dead_letter_jsonl: list[Path] | None = None,
) -> dict[str, Any]:
    ledger = Ledger(ledger_path)
    source_sessions = _source_sessions(source_store, project=project, provider=provider)
    source_natural_ids = {
        _session_natural_id(str(session.get("session_id_hash") or ""))
        for session in source_sessions
        if str(session.get("session_id_hash") or "")
    }
    episodic_rows = _projection_rows(
        ledger,
        project=project,
        extraction_level=EXTRACTION_LEVEL_EPISODIC,
    )
    entity_rows = _projection_rows(
        ledger,
        project=project,
        extraction_level=EXTRACTION_LEVEL_ENTITY,
    )
    # The ledger projection_state table has no provider column, so provider
    # scoping only happens via intersection with the selected source set.
    # Restrict entity_rows to the selected sources before computing
    # provider-scoped recency/latest metrics below.
    scoped_entity_rows = _rows_for_sessions(entity_rows, source_natural_ids)
    episodic_ids = _projected_session_natural_ids(episodic_rows)
    invalid_entity_ids = source_natural_ids & _source_invalid_session_natural_ids(entity_rows)
    valid_source_natural_ids = source_natural_ids - invalid_entity_ids
    entity_ids = _projected_session_natural_ids(entity_rows) - invalid_entity_ids
    entity_backlog = sorted(valid_source_natural_ids - entity_ids)
    unprojected_started_at = [
        str(session.get("started_at") or "")
        for session in source_sessions
        if _session_natural_id(str(session.get("session_id_hash") or "")) in entity_backlog
        and str(session.get("started_at") or "")
    ]
    progress = _summarize_progress(progress_jsonl or [])
    dead_letter = _summarize_dead_letters(dead_letter_jsonl or [])

    source_count = len(source_sessions)
    valid_source_count = len(valid_source_natural_ids)
    entity_projected = len(valid_source_natural_ids & entity_ids)
    episodic_projected = len(source_natural_ids & episodic_ids)
    now = datetime.now(timezone.utc)
    return {
        "schema_version": GRAPH_PROJECTION_STATUS_SCHEMA_VERSION,
        "status": "ok",
        "filters": {
            "project_set": bool(project),
            "project_ref": _project_ref(project),
            "provider": provider,
        },
        "source": {
            "session_count": source_count,
        },
        "projection_state": {
            "episodic_session_projected": episodic_projected,
            "entity_session_projected": entity_projected,
            "entity_session_backlog": len(entity_backlog),
            "entity_source_invalid": len(invalid_entity_ids),
            "entity_valid_source_sessions": valid_source_count,
            "entity_coverage_ratio": (entity_projected / valid_source_count) if valid_source_count else 0.0,
            "latest_entity_projected_at": _latest_projected_at(scoped_entity_rows),
            "entity_projected_last_1h": _recent_count(scoped_entity_rows, now=now, seconds=3600),
            "entity_projected_last_24h": _recent_count(scoped_entity_rows, now=now, seconds=86400),
        },
        "lag": {
            "oldest_unprojected_started_at": min(unprojected_started_at) if unprojected_started_at else "",
            "newest_unprojected_started_at": max(unprojected_started_at) if unprojected_started_at else "",
            "oldest_unprojected_age_seconds": _oldest_age_seconds(unprojected_started_at, now=now),
        },
        "progress": progress,
        "dead_letter": dead_letter,
        "raw_paths_printed": False,
    }


def _source_sessions(
    source_store: CouchDBSourceStore,
    *,
    project: str,
    provider: str,
) -> list[dict[str, Any]]:
    sessions = source_store.find_by_type(
        SourceDocType.TRANSCRIPT_SESSION,
        fields=["session_id_hash", "project", "provider", "started_at"],
    )
    filtered = _filter_sessions(sessions, project=project, provider=provider)
    filtered.sort(key=lambda session: str(session.get("session_id_hash") or ""))
    return filtered


def _projection_rows(
    ledger: Ledger,
    *,
    project: str,
    extraction_level: str,
) -> list[dict[str, str]]:
    clauses = ["entity_type = ?", "extraction_level = ?"]
    params = ["Session", extraction_level]
    if project:
        clauses.append("project = ?")
        params.append(project)
    with ledger._connect() as connection:
        if not _table_exists(connection, "llm_brain_graph_projection_state"):
            return []
        rows = connection.execute(
            """
            SELECT natural_id, extraction_level, upsert_result, projected_at
            FROM llm_brain_graph_projection_state
            WHERE """ + " AND ".join(clauses),
            tuple(params),
        ).fetchall()
    return [
        {
            "natural_id": str(row["natural_id"]),
            "extraction_level": str(row["extraction_level"]),
            "upsert_result": str(row["upsert_result"]),
            "projected_at": str(row["projected_at"]),
        }
        for row in rows
    ]


def _rows_for_sessions(
    rows: list[dict[str, str]], natural_ids: set[str]
) -> list[dict[str, str]]:
    return [row for row in rows if str(row.get("natural_id") or "") in natural_ids]


def _projected_session_natural_ids(rows: list[dict[str, str]]) -> set[str]:
    return {str(row.get("natural_id") or "") for row in rows if str(row.get("natural_id") or "")}


def _source_invalid_session_natural_ids(rows: list[dict[str, str]]) -> set[str]:
    return {
        str(row.get("natural_id") or "")
        for row in rows
        if str(row.get("natural_id") or "")
        and str(row.get("upsert_result") or "") in SOURCE_INVALID_UPSERT_RESULTS
    }


def _session_natural_id(session_id_hash: str) -> str:
    return str(session_id_hash or "").replace(":", "_")


def _latest_projected_at(rows: list[dict[str, str]]) -> str:
    values = [str(row.get("projected_at") or "") for row in rows if str(row.get("projected_at") or "")]
    return max(values) if values else ""


def _recent_count(rows: list[dict[str, str]], *, now: datetime, seconds: int) -> int:
    count = 0
    for row in rows:
        parsed = _parse_time(str(row.get("projected_at") or ""))
        if parsed is not None and (now - parsed).total_seconds() <= seconds:
            count += 1
    return count


def _oldest_age_seconds(values: list[str], *, now: datetime) -> int:
    parsed = [_parse_time(value) for value in values]
    valid = [value for value in parsed if value is not None]
    if not valid:
        return 0
    return max(0, int((now - min(valid)).total_seconds()))


def _parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _summarize_progress(paths: list[Path]) -> dict[str, Any]:
    last_progress: dict[str, Any] = {}
    elapsed_values: list[int] = []
    event_counts: Counter[str] = Counter()
    for payload in _iter_jsonl(paths):
        event = str(payload.get("event") or "")
        if event:
            event_counts[event] += 1
        if event == "progress":
            last_progress = payload
            try:
                elapsed_values.append(int(payload.get("elapsed_ms") or 0))
            except (TypeError, ValueError):
                pass
    return {
        "event_counts": dict(sorted(event_counts.items())),
        "last_index": int(last_progress.get("index") or 0) if last_progress else 0,
        "selected": int(last_progress.get("selected") or 0) if last_progress else 0,
        "projected": int(last_progress.get("projected") or 0) if last_progress else 0,
        "skipped_resumed": int(last_progress.get("skipped_resumed") or 0) if last_progress else 0,
        "failed": int(last_progress.get("failed") or 0) if last_progress else 0,
        "avg_checkpoint_elapsed_ms": int(sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0,
    }


def _summarize_dead_letters(paths: list[Path]) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    total = 0
    for payload in _iter_jsonl(paths):
        total += 1
        reasons[str(payload.get("reason_code") or "unknown")] += 1
    return {
        "count": total,
        "failure_reasons": dict(sorted(reasons.items())),
    }


def _iter_jsonl(paths: list[Path]):
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or not stripped.startswith("{"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload
