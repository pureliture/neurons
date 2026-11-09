from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.document_model import SourceDocType
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    SourceRevisionSnapshotChangedError,
)
from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore
from agent_knowledge.ledger import Ledger

from .ledger_adapter import (
    EXTRACTION_LEVEL_ENTITY,
    EXTRACTION_LEVEL_EPISODIC,
    LedgerGraphProjectionStateStore,
    LedgerSessionMemoryArtifactStore,
)
from .models import PROJECTION_SCHEMA_VERSION
from .projection import GraphProjectionWorker
from .runtime import (
    INCONSISTENT_HERMES_PROJECT_SCOPE,
    InconsistentHermesProjectScope,
    session_episode_from_couchdb_source,
    session_source_revision_from_couchdb_source,
)
from .runtime_graph import build_graph_adapter_from_env

COUCHDB_GRAPH_PROJECTION_SCHEMA_VERSION = "llm_brain_couchdb_graph_projection.v1"
GRAPH_PROJECTION_CURSOR_SCHEMA_VERSION = "llm_brain_graph_projection_cursor.v1"
GRAPH_SELECTION_PAGE_SIZE = 256
MAX_GRAPH_SELECTION_SIZE = 1000
MAX_GRAPH_SELECTION_PROJECTS = 256
GRAPH_SELECTION_SNAPSHOT_ATTEMPTS = 2
SELECTION_ERROR_FIELD = "selection_error"


@dataclass(frozen=True)
class _SessionSelection:
    sessions: list[dict[str, Any]]
    source_count: int
    eligible_count: int
    pending_count: int
    unresolved_count: int = 0
    unresolved_reasons: dict[str, int] = field(default_factory=dict)
    unresolved: list[dict[str, Any]] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-graph-project")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--couchdb-url", default=os.environ.get("COUCHDB_URL", ""))
    parser.add_argument("--couchdb-db", default=os.environ.get("COUCHDB_DB", "transcript_source"))
    parser.add_argument("--couchdb-user", default=os.environ.get("COUCHDB_USER", ""))
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    parser.add_argument("--enable-graph", action="store_true")
    parser.add_argument("--graph-required", action="store_true")
    parser.add_argument("--extract-entities", action="store_true")
    parser.add_argument("--reextract-entities", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--dead-letter-jsonl", default="")
    parser.add_argument("--progress-jsonl", default="")
    parser.add_argument("--report-every", type=int, default=25)
    parser.add_argument(
        "--max-projects",
        type=int,
        default=0,
        help="Stop after this many non-resumed successful graph upserts. 0 means no cap.",
    )
    parser.add_argument("--runtime-dir", default="")
    args = parser.parse_args(argv)

    try:
        source_store = _build_source_store(
            couchdb_url=args.couchdb_url,
            couchdb_db=args.couchdb_db,
            couchdb_user=args.couchdb_user,
            couchdb_password_env=args.couchdb_password_env,
        )
        report = run_couchdb_projection(
            ledger_path=Path(args.ledger),
            source_store=source_store,
            limit=int(args.limit),
            project=str(args.project or ""),
            provider=str(args.provider or ""),
            enable_graph=bool(args.enable_graph),
            graph_required=bool(args.graph_required),
            extract_entities=(True if (args.extract_entities or args.reextract_entities) else None),
            reextract_entities=bool(args.reextract_entities),
            resume=not bool(args.no_resume),
            dead_letter_jsonl=Path(args.dead_letter_jsonl) if args.dead_letter_jsonl else None,
            progress_jsonl=Path(args.progress_jsonl) if args.progress_jsonl else None,
            report_every=int(args.report_every),
            max_projects=int(args.max_projects),
            runtime_dir=Path(args.runtime_dir) if args.runtime_dir else None,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": COUCHDB_GRAPH_PROJECTION_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "message": "couchdb graph projection failed",
                    "raw_paths_printed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    # 실패/부분실패는 non-zero exit로 알린다. ok/already_running은 정상 종료.
    if report.get("status") in ("failed", "partial"):
        return 1
    return 0


def run_couchdb_projection(
    *,
    ledger_path: Path,
    source_store: CouchDBSourceStore,
    limit: int,
    project: str = "",
    provider: str = "",
    enable_graph: bool,
    graph_required: bool,
    extract_entities: bool | None = None,
    reextract_entities: bool = False,
    resume: bool = True,
    dead_letter_jsonl: Path | None = None,
    progress_jsonl: Path | None = None,
    report_every: int = 25,
    max_projects: int = 0,
    graph_adapter: Any | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    lock_handle, locked_report = _acquire_runtime_lock(runtime_dir)
    if locked_report is not None:
        return locked_report
    # lock 획득 직후부터 전체를 try/finally로 감싸 Ledger/store/adapter/selection
    # 구성 중 예외가 나도 lock file handle release를 보장한다.
    try:
        ledger = Ledger(ledger_path)
        artifact_store = LedgerSessionMemoryArtifactStore(ledger)
        projection_state_store = LedgerGraphProjectionStateStore(ledger)
        graph = graph_adapter or build_graph_adapter_from_env(
            environ=_graph_environ(extract_entities=extract_entities, reextract_entities=reextract_entities),
            enable_flag=True if enable_graph else None,
            required_flag=bool(graph_required),
        )
        worker = GraphProjectionWorker(graph, projection_state_store=projection_state_store)
        target_level = (
            EXTRACTION_LEVEL_ENTITY
            if bool(getattr(graph, "_extract_entities", False))
            else EXTRACTION_LEVEL_EPISODIC
        )
        cursor_scope_ref = _scheduler_cursor_scope_ref(
            project=project,
            provider=provider,
            extraction_level=target_level,
            resume=resume,
            include_current=bool(reextract_entities),
        )
        cursor_offset = _read_scheduler_cursor(
            runtime_dir,
            scope_ref=cursor_scope_ref,
        )
        selection = _select_sessions_bounded(
            source_store,
            project=project,
            provider=provider,
            limit=limit,
            projection_state_store=projection_state_store if resume else None,
            extraction_level=target_level if resume else None,
            include_current=bool(reextract_entities),
            cursor_offset=cursor_offset,
        )
        effective_cursor_offset = (
            cursor_offset % selection.eligible_count if selection.eligible_count else 0
        )
        sessions = selection.sessions
        total_available = selection.source_count
        report_every = max(1, int(report_every))
        max_projects = max(0, int(max_projects))

        durations: list[int] = []
        by_provider: Counter[str] = Counter()
        by_project: Counter[str] = Counter()
        failure_reasons: Counter[str] = Counter()
        projected = duplicates = failed = skipped_resumed = skipped_disabled = 0
        skipped_inconsistent = 0
        processed = 0
        stopped_after_max_projects = False
        started = time.monotonic()
        started_at = _utc_now_iso()
        run_id = "run:" + hashlib.sha256(
            f"{time.time_ns()}:{os.getpid()}".encode("utf-8")
        ).hexdigest()

        for unresolved_session in selection.unresolved:
            _write_dead_letter(
                dead_letter_jsonl,
                session=unresolved_session,
                run_id=run_id,
                reason_code=str(unresolved_session.get(SELECTION_ERROR_FIELD) or ""),
                stage="select",
            )

        _write_jsonl(
            progress_jsonl,
            {
                "event": "start",
                "run_id": run_id,
                "selected": len(sessions),
                "total_available": total_available,
                "unresolved_sessions": selection.unresolved_count,
                "limit": int(limit),
                "project_set": bool(project),
                "project_ref": _project_ref(project),
                "provider": provider,
                "target_extraction_level": target_level,
                "started_at": started_at,
            },
        )

        for index, session in enumerate(sessions, start=1):
            processed = index
            item_started = time.monotonic()
            session_project = str(session.get("project") or "")
            session_provider = str(session.get("provider") or "")
            session_id_hash = str(session.get("session_id_hash") or "")
            by_project[session_project] += 1
            by_provider[session_provider] += 1
            status = "unknown"
            reason = ""
            try:
                episode = session_episode_from_couchdb_source(
                    session_id_hash=session_id_hash,
                    source_store=source_store,
                    artifact_store=artifact_store,
                    extractor_version="couchdb-graph-project.1",
                )
                if resume and not reextract_entities:
                    episode_source_hash = str(episode.payload.get("source_hash") or "")
                    if projection_state_store.has_projected_source_hash(
                        session_project,
                        episode.natural_id,
                        episode_source_hash,
                        extraction_level=target_level,
                        entity_type="Session",
                        include_source_invalid=target_level != EXTRACTION_LEVEL_ENTITY,
                    ):
                        skipped_resumed += 1
                        status = "skipped_resumed"
                    else:
                        status, reason, p, d, sd, f = _project_one(worker, episode)
                        projected += p
                        duplicates += d
                        skipped_disabled += sd
                        failed += f

                else:
                    status, reason, p, d, sd, f = _project_one(worker, episode)
                    projected += p
                    duplicates += d
                    skipped_disabled += sd
                    failed += f
                if reason:
                    failure_reasons[reason] += 1
                    _write_dead_letter(
                        dead_letter_jsonl,
                        session=session,
                        run_id=run_id,
                        reason_code=reason,
                        stage="project",
                    )
            except InconsistentHermesProjectScope:
                # Deterministic source-scope conflict: retrying cannot succeed,
                # so count it separately instead of failing/dead-lettering, and
                # pin the current source revision as source_invalid so resume and
                # the status CLI stop treating it as backlog until the source
                # changes.
                skipped_inconsistent += 1
                status = "skipped_inconsistent"
                reason = INCONSISTENT_HERMES_PROJECT_SCOPE
                _mark_session_source_invalid(
                    projection_state_store,
                    session=session,
                    source_store=source_store,
                    reason=reason,
                )
            except Exception as exc:
                failed += 1
                status = "failed"
                reason = type(exc).__name__
                failure_reasons[reason] += 1
                _write_dead_letter(
                    dead_letter_jsonl,
                    session=session,
                    run_id=run_id,
                    reason_code=reason,
                    stage="materialize_or_project",
                )
            elapsed_ms = int((time.monotonic() - item_started) * 1000)
            durations.append(elapsed_ms)
            if (
                index == 1
                or index % report_every == 0
                or status in ("failed", "skipped_inconsistent")
                or index == len(sessions)
            ):
                _write_jsonl(
                    progress_jsonl,
                    {
                        "event": "progress",
                        "run_id": run_id,
                        "index": index,
                        "selected": len(sessions),
                        "status": status,
                        "reason_code": reason,
                        "elapsed_ms": elapsed_ms,
                        "project_ref": _project_ref(session_project),
                        "provider": session_provider,
                        "projected": projected,
                        "duplicates": duplicates,
                        "failed": failed,
                        "skipped_resumed": skipped_resumed,
                        "skipped_disabled": skipped_disabled,
                        "skipped_inconsistent": skipped_inconsistent,
                    },
                )
            if max_projects and (projected + duplicates) >= max_projects:
                stopped_after_max_projects = True
                break

        elapsed_total_ms = int((time.monotonic() - started) * 1000)
        attempted = processed
        status = "ok" if failed == 0 else ("partial" if projected or duplicates or skipped_resumed else "failed")
        if status == "ok" and selection.unresolved_count:
            # Healthy sessions were processed, but excluded sessions still need attention.
            status = "partial"
        removed_from_pending = (
            0
            if reextract_entities
            else min(attempted, projected + duplicates + skipped_resumed)
        )
        cursor_advance = max(0, attempted - removed_from_pending)
        next_cursor_offset = effective_cursor_offset + cursor_advance
        _write_scheduler_cursor(
            runtime_dir,
            scope_ref=cursor_scope_ref,
            offset=next_cursor_offset,
        )
        _write_jsonl(
            progress_jsonl,
            {
                "event": "complete",
                "run_id": run_id,
                "status": status,
                "selected": len(sessions),
                "attempted": attempted,
                "projected": projected,
                "duplicates": duplicates,
                "failed": failed,
                "skipped_resumed": skipped_resumed,
                "skipped_disabled": skipped_disabled,
                "skipped_inconsistent": skipped_inconsistent,
                "unresolved_sessions": selection.unresolved_count,
                "elapsed_total_ms": elapsed_total_ms,
                "project_set": bool(project),
                "project_ref": _project_ref(project),
                "provider": provider,
                "target_extraction_level": target_level,
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
            },
        )
        return {
            "schema_version": COUCHDB_GRAPH_PROJECTION_SCHEMA_VERSION,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "status": status,
            # The runtime status command exposes the same one-way reference.
            # Keep the raw progress run id private while allowing an activation
            # guard to bind its postcheck to this exact projection execution.
            "run_ref": _project_ref(run_id),
            "canonical_counts": {
                "source_sessions": total_available,
                "eligible_sessions": selection.eligible_count,
                "selected_sessions": len(sessions),
            },
            "filters": {
                "project_set": bool(project),
                "project_ref": _project_ref(project),
                "provider": provider,
            },
            "selection": {
                "unresolved_sessions": selection.unresolved_count,
                "unresolved_dead_lettered": len(selection.unresolved),
                "unresolved_reasons": dict(selection.unresolved_reasons),
            },
            "limit": int(limit),
            "truncated": bool(
                selection.eligible_count > len(sessions)
                or stopped_after_max_projects
            ),
            "graph_enabled": bool(enable_graph),
            "target_extraction_level": target_level,
            "runtime_lock": {
                "enabled": runtime_dir is not None,
                "acquired": runtime_dir is not None,
            },
            "scheduler": {
                "persistent_cursor_enabled": runtime_dir is not None,
                "scope_ref": cursor_scope_ref,
                "cursor_offset": effective_cursor_offset,
                "cursor_advance": cursor_advance,
            },
            "projection": {
                "attempted": attempted,
                "projected": projected,
                "duplicates": duplicates,
                "skipped_resumed": skipped_resumed,
                "skipped_disabled": skipped_disabled,
                "skipped_inconsistent": skipped_inconsistent,
                "failed": failed,
                "failure_rate": (failed / attempted) if attempted else 0.0,
                "failure_reasons": dict(sorted(failure_reasons.items())),
                "stopped_after_max_projects": stopped_after_max_projects,
            },
            "metrics": {
                "avg_ms": int(sum(durations) / len(durations)) if durations else 0,
                "p95_ms": _p95(durations),
                "elapsed_total_ms": elapsed_total_ms,
            },
            "by_provider": dict(sorted(by_provider.items())),
            "project_count": len(by_project),
            "raw_paths_printed": False,
        }
    finally:
        if lock_handle is not None:
            _release_runtime_lock(lock_handle)


def _acquire_runtime_lock(runtime_dir: Path | None) -> tuple[Any | None, dict[str, Any] | None]:
    if runtime_dir is None:
        return None, None
    import fcntl

    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = (runtime_dir / "graph-project.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return None, {
            "schema_version": COUCHDB_GRAPH_PROJECTION_SCHEMA_VERSION,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "status": "already_running",
            "runtime_lock": {
                "enabled": True,
                "acquired": False,
            },
            "mutation_performed": False,
            "raw_paths_printed": False,
        }
    return lock_handle, None


def _release_runtime_lock(lock_handle: Any) -> None:
    import fcntl

    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock_handle.close()


def _scheduler_cursor_scope_ref(
    *,
    project: str,
    provider: str,
    extraction_level: str,
    resume: bool,
    include_current: bool,
) -> str:
    payload = json.dumps(
        {
            "extraction_level": extraction_level,
            "include_current": bool(include_current),
            "project": project,
            "provider": provider,
            "resume": bool(resume),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_scheduler_cursor(runtime_dir: Path | None, *, scope_ref: str) -> int:
    if runtime_dir is None:
        return 0
    path = runtime_dir / "graph-projection-cursor.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("graph scheduler cursor is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != GRAPH_PROJECTION_CURSOR_SCHEMA_VERSION:
        raise ValueError("graph scheduler cursor is invalid")
    scopes = payload.get("scopes")
    if not isinstance(scopes, dict):
        raise ValueError("graph scheduler cursor is invalid")
    entry = scopes.get(scope_ref)
    if entry is None:
        return 0
    if not isinstance(entry, dict):
        raise ValueError("graph scheduler cursor is invalid")
    offset = entry.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("graph scheduler cursor is invalid")
    return offset


def _write_scheduler_cursor(
    runtime_dir: Path | None,
    *,
    scope_ref: str,
    offset: int,
) -> None:
    if runtime_dir is None:
        return
    path = runtime_dir / "graph-projection-cursor.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("graph scheduler cursor is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != GRAPH_PROJECTION_CURSOR_SCHEMA_VERSION:
            raise ValueError("graph scheduler cursor is invalid")
        scopes = payload.get("scopes")
        if not isinstance(scopes, dict):
            raise ValueError("graph scheduler cursor is invalid")
    else:
        payload = {
            "schema_version": GRAPH_PROJECTION_CURSOR_SCHEMA_VERSION,
            "scopes": {},
        }
        scopes = payload["scopes"]
    scopes[scope_ref] = {"offset": max(0, int(offset))}
    temporary = runtime_dir / f".graph-projection-cursor.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _build_source_store(
    *,
    couchdb_url: str,
    couchdb_db: str,
    couchdb_user: str,
    couchdb_password_env: str,
    couchdb_password: str | None = None,
) -> CouchDBHttpSourceStore:
    if not couchdb_url:
        raise ValueError("COUCHDB_URL is required")
    password = (
        os.environ.get(couchdb_password_env, "")
        if couchdb_password is None
        else str(couchdb_password)
    )
    return CouchDBHttpSourceStore(
        base_url=couchdb_url,
        db=couchdb_db,
        auth_header=_auth_header(couchdb_user, password),
    )


def _auth_header(user: str, password: str) -> str:
    if not user:
        return ""
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _graph_environ(*, extract_entities: bool | None, reextract_entities: bool) -> dict[str, str] | None:
    if extract_entities is None and not reextract_entities:
        return None
    graph_environ = dict(os.environ)
    if extract_entities is not None:
        graph_environ["LLM_BRAIN_GRAPH_EXTRACT_ENTITIES"] = "true" if extract_entities else "false"
    if reextract_entities:
        graph_environ["LLM_BRAIN_GRAPH_EXTRACT_ENTITIES"] = "true"
        graph_environ["LLM_BRAIN_GRAPH_FORCE_REEXTRACT_ENTITIES"] = "true"
    return graph_environ


def _select_sessions(
    source_store: CouchDBSourceStore,
    *,
    project: str,
    provider: str,
    limit: int,
    projection_state_store: LedgerGraphProjectionStateStore | None = None,
    extraction_level: str | None = None,
    include_current: bool = False,
) -> list[dict[str, Any]]:
    return _select_sessions_bounded(
        source_store,
        project=project,
        provider=provider,
        limit=limit,
        projection_state_store=projection_state_store,
        extraction_level=extraction_level,
        include_current=include_current,
    ).sessions


def _select_sessions_bounded(
    source_store: CouchDBSourceStore,
    *,
    project: str,
    provider: str,
    limit: int,
    projection_state_store: LedgerGraphProjectionStateStore | None = None,
    extraction_level: str | None = None,
    include_current: bool = False,
    cursor_offset: int = 0,
) -> _SessionSelection:
    """Select a bounded fair window without retaining source or projection history.

    Two bounded CouchDB passes are used because project fairness depends on the
    current-projection count for every selected project. Only per-project counts,
    the bounded result window, and one source page are retained in memory.
    """
    requested = int(limit)
    selection_limit = (
        min(requested, MAX_GRAPH_SELECTION_SIZE)
        if requested > 0
        else MAX_GRAPH_SELECTION_SIZE
    )

    for _attempt in range(GRAPH_SELECTION_SNAPSHOT_ATTEMPTS):
        project_counts: dict[str, dict[str, int]] = {}
        source_count = 0
        first_digest = hashlib.sha256()

        unresolved_count = 0
        for session in _iter_session_revisions(
            source_store,
            project=project,
            provider=provider,
        ):
            source_count += 1
            if session.get(SELECTION_ERROR_FIELD):
                unresolved_count += 1
                _update_selection_digest(first_digest, session, is_current=False)
                continue
            is_current = _session_projection_is_current(
                session,
                projection_state_store=projection_state_store,
                extraction_level=extraction_level,
            )
            session_project = str(session.get("project") or "")
            counts = project_counts.setdefault(session_project, {"pending": 0, "current": 0})
            counts["current" if is_current else "pending"] += 1
            _update_selection_digest(first_digest, session, is_current=is_current)
            if len(project_counts) > MAX_GRAPH_SELECTION_PROJECTS:
                raise ValueError("graph selection exceeds the project-scope bound")

        project_order = sorted(
            project_counts,
            key=lambda value: (project_counts[value]["current"], value),
        )
        pending_counts = {
            name: values["pending"] for name, values in project_counts.items()
        }
        current_counts = {
            name: values["current"] for name, values in project_counts.items()
        }
        pending_total = sum(pending_counts.values())
        current_total = sum(current_counts.values()) if include_current else 0
        eligible_count = pending_total + current_total
        normalized_offset = max(0, int(cursor_offset)) % eligible_count if eligible_count else 0

        pending_order = [name for name in project_order if pending_counts[name] > 0]
        current_order = [name for name in project_order if current_counts[name] > 0]
        pending_positions = {name: index for index, name in enumerate(pending_order)}
        current_positions = {name: index for index, name in enumerate(current_order)}
        pending_ordinals: Counter[str] = Counter()
        current_ordinals: Counter[str] = Counter()
        observed_counts: dict[str, dict[str, int]] = {}
        second_digest = hashlib.sha256()
        observed_source_count = 0
        observed_unresolved_count = 0
        unresolved_reasons: Counter[str] = Counter()
        unresolved: list[dict[str, Any]] = []
        selected_by_slot: dict[int, dict[str, Any]] = {}

        for session in _iter_session_revisions(
            source_store,
            project=project,
            provider=provider,
        ):
            observed_source_count += 1
            if session.get(SELECTION_ERROR_FIELD):
                observed_unresolved_count += 1
                unresolved_reasons[str(session[SELECTION_ERROR_FIELD])] += 1
                # Keep the retained dead-letter window bounded like the selection itself.
                if len(unresolved) < MAX_GRAPH_SELECTION_SIZE:
                    unresolved.append(session)
                _update_selection_digest(second_digest, session, is_current=False)
                continue
            is_current = _session_projection_is_current(
                session,
                projection_state_store=projection_state_store,
                extraction_level=extraction_level,
            )
            _update_selection_digest(second_digest, session, is_current=is_current)
            session_project = str(session.get("project") or "")
            lane = "current" if is_current else "pending"
            counts = observed_counts.setdefault(session_project, {"pending": 0, "current": 0})
            counts[lane] += 1

            if lane == "current" and not include_current:
                continue
            lane_counts = current_counts if lane == "current" else pending_counts
            lane_order = current_order if lane == "current" else pending_order
            positions = current_positions if lane == "current" else pending_positions
            ordinals = current_ordinals if lane == "current" else pending_ordinals
            ordinal = ordinals[session_project]
            ordinals[session_project] += 1
            rank = _round_robin_rank(
                ordinal,
                session_project,
                project_order=lane_order,
                project_positions=positions,
                project_counts=lane_counts,
            )
            absolute_rank = rank + (pending_total if lane == "current" else 0)
            slot = (absolute_rank - normalized_offset) % eligible_count if eligible_count else 0
            if slot < selection_limit:
                if slot in selected_by_slot:
                    raise ValueError("graph selection source contains duplicate session identity")
                selected_by_slot[slot] = session

        if (
            source_count != observed_source_count
            or unresolved_count != observed_unresolved_count
            or project_counts != observed_counts
            or first_digest.digest() != second_digest.digest()
        ):
            continue

        selected = [selected_by_slot[index] for index in sorted(selected_by_slot)]
        return _SessionSelection(
            sessions=selected,
            source_count=source_count,
            eligible_count=eligible_count,
            pending_count=pending_total,
            unresolved_count=observed_unresolved_count,
            unresolved_reasons=dict(sorted(unresolved_reasons.items())),
            unresolved=unresolved,
        )

    raise ValueError("graph source changed during bounded selection; retry the projection")


def _iter_session_revisions(
    source_store: CouchDBSourceStore,
    *,
    project: str,
    provider: str,
):
    for source_session in source_store.iter_by_type(
        SourceDocType.TRANSCRIPT_SESSION,
        fields=["_id", "session_id_hash", "project", "provider"],
        page_size=GRAPH_SELECTION_PAGE_SIZE,
    ):
        session_id_hash = str(source_session.get("session_id_hash") or "")
        session_project = str(source_session.get("project") or "")
        session_provider = str(source_session.get("provider") or "")
        if not session_id_hash:
            continue
        if project and session_project != project:
            continue
        if provider and session_provider != provider:
            continue
        session = dict(source_session)
        try:
            session["source_hash"] = session_source_revision_from_couchdb_source(
                session_id_hash=session_id_hash,
                source_store=source_store,
            )
        except SourceRevisionSnapshotChangedError:
            # Global database churn is a batch-level retry, not a per-session fault.
            raise
        except SourceRevisionResolutionError as exc:
            # One corrupted or over-budget session must not stop the whole batch:
            # mark it so selection excludes it and the caller dead-letters it.
            session["source_hash"] = ""
            session[SELECTION_ERROR_FIELD] = _selection_error_code(exc)
        yield session


def _selection_error_code(exc: SourceRevisionResolutionError) -> str:
    reason_code = str(getattr(exc, "reason_code", "") or "")
    return reason_code or type(exc).__name__


def _session_projection_is_current(
    session: dict[str, Any],
    *,
    projection_state_store: LedgerGraphProjectionStateStore | None,
    extraction_level: str | None,
) -> bool:
    source_hash = str(session.get("source_hash") or "")
    if (
        not source_hash
        or projection_state_store is None
        or extraction_level is None
    ):
        return False
    return projection_state_store.has_projected_source_hash(
        str(session.get("project") or ""),
        _session_natural_id(str(session.get("session_id_hash") or "")),
        source_hash,
        extraction_level=extraction_level,
        entity_type="Session",
        include_source_invalid=extraction_level != EXTRACTION_LEVEL_ENTITY,
    )


def _update_selection_digest(digest: Any, session: dict[str, Any], *, is_current: bool) -> None:
    payload = json.dumps(
        [
            str(session.get("_id") or ""),
            str(session.get("project") or ""),
            str(session.get("provider") or ""),
            str(session.get("session_id_hash") or ""),
            str(session.get("source_hash") or ""),
            bool(is_current),
            str(session.get(SELECTION_ERROR_FIELD) or ""),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest.update(payload.encode("utf-8"))
    digest.update(b"\n")


def _round_robin_rank(
    ordinal: int,
    session_project: str,
    *,
    project_order: list[str],
    project_positions: dict[str, int],
    project_counts: dict[str, int],
) -> int:
    """Return a candidate's rank in a fair interleave using O(projects) state."""
    position = project_positions[session_project]
    completed_rounds = sum(min(ordinal, project_counts[name]) for name in project_order)
    earlier_in_round = sum(
        1
        for name in project_order
        if project_positions[name] < position and project_counts[name] > ordinal
    )
    return completed_rounds + earlier_in_round


def _session_natural_id(session_id_hash: str) -> str:
    return str(session_id_hash or "").replace(":", "_")


def _mark_session_source_invalid(
    projection_state_store: LedgerGraphProjectionStateStore,
    *,
    session: dict[str, Any],
    source_store: CouchDBSourceStore,
    reason: str,
) -> None:
    session_id_hash = str(session.get("session_id_hash") or "")
    source_hash = str(session.get("source_hash") or "")
    if not source_hash and session_id_hash:
        source_hash = session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=source_store,
        )
    projection_state_store.mark_source_invalid(
        project=str(session.get("project") or ""),
        natural_id=_session_natural_id(session_id_hash),
        source_hash=source_hash,
        reason=reason,
    )


def _filter_sessions(
    sessions: list[dict[str, Any]],
    *,
    project: str,
    provider: str,
) -> list[dict[str, Any]]:
    """Filter an already bounded/status-only source list by scope."""
    return [
        session
        for session in sessions
        if str(session.get("session_id_hash") or "")
        and (not project or str(session.get("project") or "") == project)
        and (not provider or str(session.get("provider") or "") == provider)
    ]


def _project_one(worker: GraphProjectionWorker, episode: Any) -> tuple[str, str, int, int, int, int]:
    report = worker.project_episodes([episode], resume_projected_ids=set())
    if report.failed:
        return (
            "failed",
            str(report.failures[0].get("reason_code") or "projection_failed"),
            int(report.projected),
            int(report.duplicates),
            int(report.skipped_disabled),
            int(report.failed),
        )
    # graph disabled면 upsert가 일어나지 않으므로 projected/duplicates가 0이다.
    # 이를 "duplicate"로 오보고하지 않고 skipped_disabled로 정확히 분류한다.
    if report.skipped_disabled:
        return (
            "skipped_disabled",
            "",
            int(report.projected),
            int(report.duplicates),
            int(report.skipped_disabled),
            0,
        )
    return (
        "projected" if report.projected else "duplicate",
        "",
        int(report.projected),
        int(report.duplicates),
        0,
        0,
    )


def _write_dead_letter(
    path: Path | None,
    *,
    session: dict[str, Any],
    run_id: str = "",
    reason_code: str,
    stage: str,
) -> None:
    _write_jsonl(
        path,
        {
            "session_id_hash": str(session.get("session_id_hash") or ""),
            "run_id": run_id,
            "project_ref": _project_ref(str(session.get("project") or "")),
            "provider": str(session.get("provider") or ""),
            "reason_code": reason_code,
            "stage": stage,
        },
    )


def _write_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return int(ordered[index])


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_ref(project: str) -> str:
    value = str(project or "")
    if not value:
        return ""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
