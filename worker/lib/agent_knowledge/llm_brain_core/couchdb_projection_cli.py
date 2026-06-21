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
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.document_model import SourceDocType
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
from .runtime import session_episode_from_couchdb_source
from .runtime_graph import build_graph_adapter_from_env

COUCHDB_GRAPH_PROJECTION_SCHEMA_VERSION = "llm_brain_couchdb_graph_projection.v1"


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
    graph_adapter: Any | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    lock_handle, locked_report = _acquire_runtime_lock(runtime_dir)
    if locked_report is not None:
        return locked_report
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
    sessions = _select_sessions(source_store, project=project, provider=provider, limit=limit)
    total_available = _count_sessions(source_store, project=project, provider=provider)
    report_every = max(1, int(report_every))

    projected_cache: dict[str, set[str]] = {}
    durations: list[int] = []
    by_provider: Counter[str] = Counter()
    by_project: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    projected = duplicates = failed = skipped_resumed = 0
    started = time.monotonic()

    _write_jsonl(
        progress_jsonl,
        {
            "event": "start",
            "selected": len(sessions),
            "total_available": total_available,
            "limit": int(limit),
        },
    )

    try:
        for index, session in enumerate(sessions, start=1):
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
                    if session_project not in projected_cache:
                        projected_cache[session_project] = set(
                            projection_state_store.list_projected_ids(
                                session_project,
                                extraction_level=target_level,
                            )
                        )
                    if episode.episode_id in projected_cache[session_project]:
                        skipped_resumed += 1
                        status = "skipped_resumed"
                    else:
                        status, reason, p, d, f = _project_one(worker, episode)
                        projected += p
                        duplicates += d
                        failed += f
                        if not f:
                            projected_cache[session_project].add(episode.episode_id)
                else:
                    status, reason, p, d, f = _project_one(worker, episode)
                    projected += p
                    duplicates += d
                    failed += f
                if reason:
                    failure_reasons[reason] += 1
                    _write_dead_letter(
                        dead_letter_jsonl,
                        session=session,
                        reason_code=reason,
                        stage="project",
                    )
            except Exception as exc:
                failed += 1
                status = "failed"
                reason = type(exc).__name__
                failure_reasons[reason] += 1
                _write_dead_letter(
                    dead_letter_jsonl,
                    session=session,
                    reason_code=reason,
                    stage="materialize_or_project",
                )
            elapsed_ms = int((time.monotonic() - item_started) * 1000)
            durations.append(elapsed_ms)
            if index == 1 or index % report_every == 0 or status == "failed" or index == len(sessions):
                _write_jsonl(
                    progress_jsonl,
                    {
                        "event": "progress",
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
                    },
                )
    finally:
        if lock_handle is not None:
            _release_runtime_lock(lock_handle)

    elapsed_total_ms = int((time.monotonic() - started) * 1000)
    attempted = len(sessions)
    status = "ok" if failed == 0 else ("partial" if projected or duplicates or skipped_resumed else "failed")
    return {
        "schema_version": COUCHDB_GRAPH_PROJECTION_SCHEMA_VERSION,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "status": status,
        "canonical_counts": {
            "source_sessions": total_available,
            "selected_sessions": attempted,
        },
        "filters": {
            "project_set": bool(project),
            "project_ref": _project_ref(project),
            "provider": provider,
        },
        "limit": int(limit),
        "truncated": bool(limit > 0 and total_available > attempted),
        "graph_enabled": bool(enable_graph),
        "target_extraction_level": target_level,
        "runtime_lock": {
            "enabled": runtime_dir is not None,
            "acquired": runtime_dir is not None,
        },
        "projection": {
            "attempted": attempted,
            "projected": projected,
            "duplicates": duplicates,
            "skipped_resumed": skipped_resumed,
            "failed": failed,
            "failure_rate": (failed / attempted) if attempted else 0.0,
            "failure_reasons": dict(sorted(failure_reasons.items())),
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


def _build_source_store(
    *,
    couchdb_url: str,
    couchdb_db: str,
    couchdb_user: str,
    couchdb_password_env: str,
) -> CouchDBHttpSourceStore:
    if not couchdb_url:
        raise ValueError("COUCHDB_URL is required")
    return CouchDBHttpSourceStore(
        base_url=couchdb_url,
        db=couchdb_db,
        auth_header=_auth_header(couchdb_user, os.environ.get(couchdb_password_env, "")),
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
) -> list[dict[str, Any]]:
    sessions = source_store.find_by_type(
        SourceDocType.TRANSCRIPT_SESSION,
        fields=["session_id_hash", "project", "provider"],
    )
    filtered = _filter_sessions(sessions, project=project, provider=provider)
    filtered.sort(key=_session_sort_key)
    if limit <= 0:
        return filtered
    return filtered[: int(limit)]


def _count_sessions(source_store: CouchDBSourceStore, *, project: str, provider: str) -> int:
    sessions = source_store.find_by_type(
        SourceDocType.TRANSCRIPT_SESSION,
        fields=["session_id_hash", "project", "provider"],
    )
    return len(_filter_sessions(sessions, project=project, provider=provider))


def _filter_sessions(
    sessions: list[dict[str, Any]],
    *,
    project: str,
    provider: str,
) -> list[dict[str, Any]]:
    filtered = [session for session in sessions if str(session.get("session_id_hash") or "")]
    if project:
        filtered = [session for session in filtered if str(session.get("project") or "") == project]
    if provider:
        filtered = [session for session in filtered if str(session.get("provider") or "") == provider]
    return filtered


def _session_sort_key(session: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(session.get("project") or ""),
        str(session.get("provider") or ""),
        str(session.get("session_id_hash") or ""),
    )


def _project_one(worker: GraphProjectionWorker, episode: Any) -> tuple[str, str, int, int, int]:
    report = worker.project_episodes([episode], resume_projected_ids=set())
    if report.failed:
        return (
            "failed",
            str(report.failures[0].get("reason_code") or "projection_failed"),
            int(report.projected),
            int(report.duplicates),
            int(report.failed),
        )
    return (
        "projected" if report.projected else "duplicate",
        "",
        int(report.projected),
        int(report.duplicates),
        0,
    )


def _write_dead_letter(
    path: Path | None,
    *,
    session: dict[str, Any],
    reason_code: str,
    stage: str,
) -> None:
    _write_jsonl(
        path,
        {
            "session_id_hash": str(session.get("session_id_hash") or ""),
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


def _project_ref(project: str) -> str:
    value = str(project or "")
    if not value:
        return ""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
