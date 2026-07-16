from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source.document_model import (
    ProjectionStatus,
    SourceDocType,
    sha256_hash,
)
from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore
from agent_knowledge.ledger import Ledger
from agent_knowledge.ledger_base import _table_exists

from .couchdb_projection_cli import _build_source_store, _filter_sessions, _project_ref
from .ledger_adapter import EXTRACTION_LEVEL_ENTITY, EXTRACTION_LEVEL_EPISODIC
from .runtime import session_source_revision_from_couchdb_source

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
    ledger = Ledger(ledger_path, read_only=True)
    source_sessions = _source_sessions(source_store, project=project, provider=provider)
    source_natural_ids = {
        _session_natural_id(str(session.get("session_id_hash") or ""))
        for session in source_sessions
        if str(session.get("session_id_hash") or "")
    }
    source_hashes = {
        _session_natural_id(str(session.get("session_id_hash") or "")): str(
            session.get("source_hash") or ""
        )
        for session in source_sessions
        if str(session.get("session_id_hash") or "")
    }
    source_state_digest = _canonical_state_digest(
        lane="source",
        items=[
            {
                "session_ref": sha256_hash(
                    _session_natural_id(str(session.get("session_id_hash") or ""))
                ),
                "source_hash": str(session.get("source_hash") or ""),
                "materialized_at": str(session.get("materialized_at") or ""),
            }
            for session in source_sessions
            if str(session.get("session_id_hash") or "")
        ],
    )
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
    scoped_episodic_rows = _rows_for_sessions(episodic_rows, source_natural_ids)
    episodic_ids = _current_projected_session_natural_ids(scoped_episodic_rows, source_hashes)
    invalid_entity_ids = source_natural_ids & _source_invalid_session_natural_ids(
        entity_rows, source_hashes
    )
    valid_source_natural_ids = source_natural_ids - invalid_entity_ids
    entity_ids = (
        _current_projected_session_natural_ids(scoped_entity_rows, source_hashes)
        - invalid_entity_ids
    )
    current_entity_rows = _current_projection_rows(scoped_entity_rows, source_hashes)
    mismatch_ids = _source_hash_mismatch_ids(
        scoped_episodic_rows, source_hashes
    ) | _source_hash_mismatch_ids(scoped_entity_rows, source_hashes)
    stale_ids = _stale_projected_ids(
        scoped_episodic_rows, source_hashes
    ) | _stale_projected_ids(scoped_entity_rows, source_hashes)
    session_memory_states = _session_memory_projection_states(
        source_store=source_store,
        source_hashes=source_hashes,
    )
    session_memory_current_ids, session_memory_mismatch_ids, session_memory_stale_ids = (
        _session_memory_projection_currentness(
            source_hashes=source_hashes,
            states=session_memory_states,
        )
    )
    graph_projection_state_digest = _graph_projection_state_digest(
        source_hashes=source_hashes,
        episodic_rows=scoped_episodic_rows,
        entity_rows=scoped_entity_rows,
    )
    session_memory_projection_state_digest = _canonical_state_digest(
        lane="session_memory",
        items=[
            {
                "session_ref": sha256_hash(natural_id),
                "projection_status": str(
                    (session_memory_states.get(natural_id) or {}).get(
                        "projection_status"
                    )
                    or ""
                ),
                "projected_source_hash": str(
                    (session_memory_states.get(natural_id) or {}).get(
                        "projected_source_hash"
                    )
                    or ""
                ),
                "materialized_at": str(
                    (session_memory_states.get(natural_id) or {}).get(
                        "materialized_at"
                    )
                    or ""
                ),
                "active_content_hash": str(
                    (session_memory_states.get(natural_id) or {}).get(
                        "active_content_hash"
                    )
                    or ""
                ),
            }
            for natural_id in sorted(source_hashes)
        ],
    )
    source_projection_state_digest = _canonical_state_digest(
        lane="source_projection_join",
        items=[
            {
                "source_state_digest": source_state_digest,
                "graph_projection_state_digest": graph_projection_state_digest,
                "session_memory_projection_state_digest": (
                    session_memory_projection_state_digest
                ),
            }
        ],
    )
    session_memory_noncurrent_ids = source_natural_ids - session_memory_current_ids
    mismatch_ids |= session_memory_mismatch_ids
    stale_ids |= session_memory_stale_ids
    entity_backlog = sorted(valid_source_natural_ids - entity_ids)
    unprojected_started_at = [
        str(session.get("started_at") or "")
        for session in source_sessions
        if _session_natural_id(str(session.get("session_id_hash") or "")) in entity_backlog
        and str(session.get("started_at") or "")
    ]
    progress = _summarize_progress(
        progress_jsonl or [],
        project=project,
        provider=provider,
    )
    latest_run_id = str(progress.pop("_latest_run_id", "") or "")
    latest_entity_run = progress.get("latest_entity_run") or {}
    latest_entity_run_id = str(latest_entity_run.pop("_run_id", "") or "")
    dead_letter = _summarize_dead_letters(
        dead_letter_jsonl or [],
        run_id=latest_run_id,
    )
    entity_dead_letter = (
        _summarize_dead_letters(dead_letter_jsonl or [], run_id=latest_entity_run_id)
        if latest_entity_run_id
        else {"count": 0}
    )
    latest_entity_run["dead_letter_count"] = int(entity_dead_letter["count"])

    source_count = len(source_sessions)
    valid_source_count = len(valid_source_natural_ids)
    entity_projected = len(valid_source_natural_ids & entity_ids)
    episodic_projected = len(source_natural_ids & episodic_ids)
    now = datetime.now(timezone.utc)
    artifact_age = _artifact_age_summary(
        ledger,
        source_sessions=source_sessions,
        now=now,
    )
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
            "source_state_digest": source_state_digest,
            "graph_projection_state_digest": graph_projection_state_digest,
            "session_memory_projection_state_digest": (
                session_memory_projection_state_digest
            ),
            "source_projection_state_digest": source_projection_state_digest,
            "episodic_session_projected": episodic_projected,
            "episodic_session_noncurrent": source_count - episodic_projected,
            "entity_session_projected": entity_projected,
            "entity_session_backlog": len(entity_backlog),
            "entity_source_invalid": len(invalid_entity_ids),
            "entity_valid_source_sessions": valid_source_count,
            "entity_coverage_ratio": (entity_projected / valid_source_count) if valid_source_count else 0.0,
            "source_hash_mismatch_count": len(mismatch_ids),
            "stale_projected_session_count": len(stale_ids),
            "session_memory_projection_current_count": len(
                source_natural_ids & session_memory_current_ids
            ),
            "session_memory_projection_noncurrent_count": len(
                session_memory_noncurrent_ids
            ),
            "session_memory_source_hash_mismatch_count": len(
                session_memory_mismatch_ids
            ),
            "session_memory_stale_projected_session_count": len(
                session_memory_stale_ids
            ),
            "latest_entity_projected_at": _latest_projected_at(current_entity_rows),
            "entity_projected_last_1h": _recent_count(current_entity_rows, now=now, seconds=3600),
            "entity_projected_last_24h": _recent_count(current_entity_rows, now=now, seconds=86400),
        },
        "lag": {
            "oldest_unprojected_started_at": min(unprojected_started_at) if unprojected_started_at else "",
            "newest_unprojected_started_at": max(unprojected_started_at) if unprojected_started_at else "",
            "oldest_unprojected_age_seconds": _oldest_age_seconds(unprojected_started_at, now=now),
        },
        "artifact_age": artifact_age,
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
        fields=[
            "session_id_hash",
            "project",
            "provider",
            "started_at",
            "observed_at_start",
            "source_hash",
            "materialized_at",
        ],
    )
    filtered = _filter_sessions(sessions, project=project, provider=provider)
    for session in filtered:
        session_id_hash = str(session.get("session_id_hash") or "")
        session["source_hash"] = session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=source_store,
        )
    filtered.sort(key=lambda session: str(session.get("session_id_hash") or ""))
    return filtered


def _session_memory_projection_states(
    *,
    source_store: CouchDBSourceStore,
    source_hashes: dict[str, str],
) -> dict[str, dict[str, Any]]:
    states = source_store.find_by_type(
        SourceDocType.PROJECTION_STATE,
        fields=[
            "session_id_hash",
            "projection_status",
            "projected_source_hash",
            "materialized_at",
            "active_content_hash",
        ],
    )
    return {
        _session_natural_id(str(state.get("session_id_hash") or "")): state
        for state in states
        if _session_natural_id(str(state.get("session_id_hash") or ""))
        in source_hashes
    }


def _session_memory_projection_currentness(
    *,
    source_hashes: dict[str, str],
    states: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str], set[str]]:
    """Compare canonical CouchDB projection state with the current source revision."""

    current_ids: set[str] = set()
    mismatch_ids: set[str] = set()
    stale_projected_ids: set[str] = set()
    for natural_id, current_source_hash in source_hashes.items():
        state = states.get(natural_id)
        status = str((state or {}).get("projection_status") or "")
        projected_source_hash = str(
            (state or {}).get("projected_source_hash") or ""
        )
        is_current = bool(
            state is not None
            and current_source_hash
            and status == ProjectionStatus.PROJECTED
            and projected_source_hash == current_source_hash
        )
        if is_current:
            current_ids.add(natural_id)
            continue
        if state is not None and current_source_hash and (
            (
                bool(projected_source_hash)
                and projected_source_hash != current_source_hash
            )
            or (
                status == ProjectionStatus.PROJECTED
                and not projected_source_hash
            )
        ):
            mismatch_ids.add(natural_id)
        if status == ProjectionStatus.PROJECTED:
            stale_projected_ids.add(natural_id)
    return current_ids, mismatch_ids, stale_projected_ids


def _canonical_state_digest(*, lane: str, items: list[dict[str, str]]) -> str:
    canonical = {
        "schema_version": "lbrain_projection_state_digest.v1",
        "lane": lane,
        "items": sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
    }
    return sha256_hash(json.dumps(canonical, sort_keys=True, separators=(",", ":")))


def _graph_projection_state_digest(
    *,
    source_hashes: dict[str, str],
    episodic_rows: list[dict[str, str]],
    entity_rows: list[dict[str, str]],
) -> str:
    by_lane = {
        EXTRACTION_LEVEL_EPISODIC: {
            str(row.get("natural_id") or ""): row for row in episodic_rows
        },
        EXTRACTION_LEVEL_ENTITY: {
            str(row.get("natural_id") or ""): row for row in entity_rows
        },
    }
    return _canonical_state_digest(
        lane="graph",
        items=[
            {
                "session_ref": sha256_hash(natural_id),
                "extraction_level": extraction_level,
                "projected_source_hash": str(
                    (by_lane[extraction_level].get(natural_id) or {}).get(
                        "source_hash"
                    )
                    or ""
                ),
                "upsert_result": str(
                    (by_lane[extraction_level].get(natural_id) or {}).get(
                        "upsert_result"
                    )
                    or ""
                ),
            }
            for natural_id in sorted(source_hashes)
            for extraction_level in (
                EXTRACTION_LEVEL_EPISODIC,
                EXTRACTION_LEVEL_ENTITY,
            )
        ],
    )


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
            SELECT natural_id, extraction_level, upsert_result, source_hash, projected_at
            FROM llm_brain_graph_projection_state
            WHERE """ + " AND ".join(clauses),
            tuple(params),
        ).fetchall()
    return [
        {
            "natural_id": str(row["natural_id"]),
            "extraction_level": str(row["extraction_level"]),
            "upsert_result": str(row["upsert_result"]),
            "source_hash": str(row["source_hash"]),
            "projected_at": str(row["projected_at"]),
        }
        for row in rows
    ]


def _rows_for_sessions(
    rows: list[dict[str, str]], natural_ids: set[str]
) -> list[dict[str, str]]:
    return [row for row in rows if str(row.get("natural_id") or "") in natural_ids]


def _current_projection_rows(
    rows: list[dict[str, str]], source_hashes: dict[str, str]
) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if (natural_id := str(row.get("natural_id") or ""))
        and bool(source_hashes.get(natural_id))
        and str(row.get("source_hash") or "") == source_hashes[natural_id]
    ]


def _current_projected_session_natural_ids(
    rows: list[dict[str, str]], source_hashes: dict[str, str]
) -> set[str]:
    return {
        str(row.get("natural_id") or "")
        for row in _current_projection_rows(rows, source_hashes)
    }


def _source_invalid_session_natural_ids(
    rows: list[dict[str, str]], source_hashes: dict[str, str]
) -> set[str]:
    return {
        str(row.get("natural_id") or "")
        for row in rows
        if str(row.get("natural_id") or "")
        and str(row.get("upsert_result") or "") in SOURCE_INVALID_UPSERT_RESULTS
        and bool(source_hashes.get(str(row.get("natural_id") or "")))
        and str(row.get("source_hash") or "")
        == source_hashes[str(row.get("natural_id") or "")]
    }


def _source_hash_mismatch_ids(
    rows: list[dict[str, str]], source_hashes: dict[str, str]
) -> set[str]:
    current_ids = _current_projected_session_natural_ids(rows, source_hashes)
    return {
        natural_id
        for row in rows
        if (natural_id := str(row.get("natural_id") or ""))
        and natural_id not in current_ids
        and bool(source_hashes.get(natural_id))
        and bool(str(row.get("source_hash") or ""))
        and str(row.get("source_hash") or "") != source_hashes[natural_id]
    }


def _stale_projected_ids(
    rows: list[dict[str, str]], source_hashes: dict[str, str]
) -> set[str]:
    current_ids = _current_projected_session_natural_ids(rows, source_hashes)
    return {
        str(row.get("natural_id") or "")
        for row in rows
        if str(row.get("natural_id") or "")
        and str(row.get("natural_id") or "") not in current_ids
    }


def _artifact_age_summary(
    ledger: Ledger,
    *,
    source_sessions: list[dict[str, Any]],
    now: datetime,
) -> dict[str, int]:
    session_ids = {
        str(session.get("session_id_hash") or "")
        for session in source_sessions
        if str(session.get("session_id_hash") or "")
    }
    if not session_ids:
        return {
            "artifact_session_count": 0,
            "artifact_missing_session_count": 0,
            "artifact_age_unknown_count": 0,
            "artifact_source_hash_mismatch_count": 0,
            "oldest_artifact_age_seconds": 0,
            "newest_artifact_age_seconds": 0,
        }
    with ledger._connect() as connection:
        if not _table_exists(connection, "llm_brain_session_memory_artifacts"):
            rows = []
        else:
            rows = connection.execute(
                "SELECT session_id_hash, artifact_json, created_at "
                "FROM llm_brain_session_memory_artifacts"
            ).fetchall()
    latest: dict[str, tuple[tuple[int, str, str, str], str, str]] = {}
    for row in rows:
        session_id_hash = str(row["session_id_hash"] or "")
        if session_id_hash not in session_ids:
            continue
        try:
            payload = json.loads(str(row["artifact_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        materialized_at = str(payload.get("materialized_at") or "")
        created_at = str(payload.get("created_at") or row["created_at"] or "")
        key = (
            int(payload.get("materialization_revision") or 0),
            materialized_at,
            str(payload.get("source_revision") or ""),
            created_at,
        )
        if session_id_hash not in latest or key > latest[session_id_hash][0]:
            latest[session_id_hash] = (
                key,
                materialized_at or created_at,
                str(payload.get("source_revision") or ""),
            )
    parsed = [_parse_time(value) for _key, value, _revision in latest.values()]
    known = [value for value in parsed if value is not None]
    ages = [max(0, int((now - value).total_seconds())) for value in known]
    current_source_hashes = {
        str(session.get("session_id_hash") or ""): str(session.get("source_hash") or "")
        for session in source_sessions
        if str(session.get("session_id_hash") or "")
    }
    source_hash_mismatches = {
        session_id_hash
        for session_id_hash, (_key, _time, artifact_source_revision) in latest.items()
        if not artifact_source_revision
        or not current_source_hashes.get(session_id_hash)
        or artifact_source_revision != current_source_hashes[session_id_hash]
    }
    return {
        "artifact_session_count": len(latest),
        "artifact_missing_session_count": len(session_ids - set(latest)),
        "artifact_age_unknown_count": len(latest) - len(known),
        "artifact_source_hash_mismatch_count": len(source_hash_mismatches),
        "oldest_artifact_age_seconds": max(ages) if ages else 0,
        "newest_artifact_age_seconds": min(ages) if ages else 0,
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


def _summarize_progress(
    paths: list[Path],
    *,
    project: str = "",
    provider: str = "",
) -> dict[str, Any]:
    payloads = list(_iter_jsonl(paths))

    def summarize(start_index: int) -> dict[str, Any]:
        latest_start = payloads[start_index] if start_index >= 0 else {}
        latest_run_id = str(latest_start.get("run_id") or "")
        if latest_run_id:
            run_payloads = [
                payload
                for payload in payloads[start_index:]
                if str(payload.get("run_id") or "") == latest_run_id
            ]
        else:
            run_payloads = []
        event_counts: Counter[str] = Counter(
            str(payload.get("event") or "")
            for payload in run_payloads
            if str(payload.get("event") or "")
        )
        progress_payloads = [
            payload
            for payload in run_payloads
            if str(payload.get("event") or "") == "progress"
        ]
        complete_payloads = [
            payload
            for payload in run_payloads
            if str(payload.get("event") or "") == "complete"
        ]
        last_progress = progress_payloads[-1] if progress_payloads else {}
        terminal = complete_payloads[-1] if complete_payloads else {}
        final = terminal or last_progress or latest_start
        elapsed_values: list[int] = []
        for payload in progress_payloads:
            try:
                elapsed_values.append(int(payload.get("elapsed_ms") or 0))
            except (TypeError, ValueError):
                pass
        scope_fields = (
            "project_set",
            "project_ref",
            "provider",
            "target_extraction_level",
        )
        scope_consistent = bool(terminal and latest_run_id) and all(
            terminal.get(field) == latest_start.get(field) for field in scope_fields
        )
        return {
            "event_counts": dict(sorted(event_counts.items())),
            "last_index": int(last_progress.get("index") or 0) if last_progress else 0,
            "selected": int(final.get("selected") or 0),
            "projected": int(final.get("projected") or 0),
            "skipped_resumed": int(final.get("skipped_resumed") or 0),
            "failed": int(final.get("failed") or 0),
            "avg_checkpoint_elapsed_ms": int(sum(elapsed_values) / len(elapsed_values))
            if elapsed_values
            else 0,
            "latest_run_completed": bool(terminal and latest_run_id),
            "latest_run_status": str(terminal.get("status") or ""),
            "latest_run_ref": _project_ref(latest_run_id) if latest_run_id else "",
            "latest_run_project_set": latest_start.get("project_set") is True,
            "latest_run_project_ref": str(latest_start.get("project_ref") or ""),
            "latest_run_provider": str(latest_start.get("provider") or ""),
            "latest_run_target_extraction_level": str(
                latest_start.get("target_extraction_level") or ""
            ),
            "latest_run_scope_consistent": scope_consistent,
            "latest_run_started_at": str(latest_start.get("started_at") or ""),
            "latest_run_completed_at": str(terminal.get("completed_at") or ""),
            "_latest_run_id": latest_run_id,
        }

    start_indexes = [
        index
        for index, payload in enumerate(payloads)
        if str(payload.get("event") or "") == "start"
    ]
    latest_start_index = start_indexes[-1] if start_indexes else -1
    summary = summarize(latest_start_index)
    expected_project_ref = _project_ref(project) if project else ""
    entity_indexes = [
        index
        for index in start_indexes
        if str(payloads[index].get("target_extraction_level") or "") == "entity"
        and (not project or payloads[index].get("project_set") is True)
        and (not project or str(payloads[index].get("project_ref") or "") == expected_project_ref)
        and str(payloads[index].get("provider") or "") == str(provider or "")
    ]
    entity_summary = summarize(entity_indexes[-1] if entity_indexes else -1)
    summary["latest_entity_run"] = {
        "event_counts": entity_summary["event_counts"],
        "selected": entity_summary["selected"],
        "projected": entity_summary["projected"],
        "failed": entity_summary["failed"],
        "completed": entity_summary["latest_run_completed"],
        "status": entity_summary["latest_run_status"],
        "run_ref": entity_summary["latest_run_ref"],
        "project_set": entity_summary["latest_run_project_set"],
        "project_ref": entity_summary["latest_run_project_ref"],
        "provider": entity_summary["latest_run_provider"],
        "target_extraction_level": entity_summary[
            "latest_run_target_extraction_level"
        ],
        "scope_consistent": entity_summary["latest_run_scope_consistent"],
        "started_at": entity_summary["latest_run_started_at"],
        "completed_at": entity_summary["latest_run_completed_at"],
        "_run_id": entity_summary["_latest_run_id"],
    }
    return summary


def _summarize_dead_letters(paths: list[Path], *, run_id: str = "") -> dict[str, Any]:
    payloads = list(_iter_jsonl(paths))
    selected = [
        payload
        for payload in payloads
        if not run_id or str(payload.get("run_id") or "") == run_id
    ]
    reasons: Counter[str] = Counter()
    for payload in selected:
        reasons[str(payload.get("reason_code") or "unknown")] += 1
    return {
        "count": len(selected),
        "total_count": len(payloads),
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
