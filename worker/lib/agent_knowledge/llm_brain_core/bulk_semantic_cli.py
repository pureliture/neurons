from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore
from agent_knowledge.ledger import Ledger

from .bulk_semantic import (
    DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS,
    DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS,
    DEFAULT_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL,
    DeterministicGraphitiSemanticWriter,
    OpenAICompatibleBulkSemanticExtractor,
    make_bulk_session_input,
)
from .couchdb_projection_cli import (
    _acquire_runtime_lock,
    _build_source_store,
    _count_sessions,
    _project_ref,
    _release_runtime_lock,
    _select_sessions,
    _write_dead_letter,
    _write_jsonl,
)
from .ledger_adapter import (
    EXTRACTION_LEVEL_ENTITY,
    LedgerGraphProjectionStateStore,
    LedgerSessionMemoryArtifactStore,
)
from .models import PROJECTION_SCHEMA_VERSION
from .runtime import session_episode_from_couchdb_source

BULK_SEMANTIC_SCHEMA_VERSION = "llm_brain_couchdb_bulk_semantic.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-graph-bulk-semantic")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--couchdb-url", default=os.environ.get("COUCHDB_URL", ""))
    parser.add_argument("--couchdb-db", default=os.environ.get("COUCHDB_DB", "transcript_source"))
    parser.add_argument("--couchdb-user", default=os.environ.get("COUCHDB_USER", ""))
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    parser.add_argument("--dead-letter-jsonl", default="")
    parser.add_argument("--progress-jsonl", default="")
    parser.add_argument("--report-every", type=int, default=25)
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--max-projects", type=int, default=0)
    parser.add_argument(
        "--max-sessions-per-call",
        type=int,
        default=_positive_int_env(
            os.environ.get("LLM_BRAIN_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL", ""),
            DEFAULT_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL,
        ),
    )
    parser.add_argument(
        "--max-session-chars",
        type=int,
        default=_positive_int_env(
            os.environ.get("LLM_BRAIN_BULK_SEMANTIC_MAX_SESSION_CHARS", ""),
            DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS,
        ),
    )
    parser.add_argument("--allow-empty-sessions", action="store_true")
    args = parser.parse_args(argv)

    try:
        source_store = _build_source_store(
            couchdb_url=args.couchdb_url,
            couchdb_db=args.couchdb_db,
            couchdb_user=args.couchdb_user,
            couchdb_password_env=args.couchdb_password_env,
        )
        report = run_couchdb_bulk_semantic_projection(
            ledger_path=Path(args.ledger),
            source_store=source_store,
            limit=int(args.limit),
            project=str(args.project or ""),
            provider=str(args.provider or ""),
            dead_letter_jsonl=Path(args.dead_letter_jsonl) if args.dead_letter_jsonl else None,
            progress_jsonl=Path(args.progress_jsonl) if args.progress_jsonl else None,
            report_every=int(args.report_every),
            runtime_dir=Path(args.runtime_dir) if args.runtime_dir else None,
            max_projects=int(args.max_projects),
            max_sessions_per_call=int(args.max_sessions_per_call),
            max_session_chars=int(args.max_session_chars),
            allow_empty_sessions=(
                bool(args.allow_empty_sessions)
                or _truthy(os.environ.get("LLM_BRAIN_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS", ""))
                or DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS
            ),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": BULK_SEMANTIC_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "message": "couchdb bulk semantic projection failed",
                    "raw_paths_printed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    # failed/partial은 non-zero로 종료해 상위 오케스트레이션(trigger wrapper 등)이
    # 실패를 성공으로 오판하지 않게 한다. already_running은 no-op이므로 0.
    return 0 if report.get("status") in {"ok", "already_running"} else 1


def run_couchdb_bulk_semantic_projection(
    *,
    ledger_path: Path,
    source_store: CouchDBSourceStore,
    limit: int,
    project: str = "",
    provider: str = "",
    dead_letter_jsonl: Path | None = None,
    progress_jsonl: Path | None = None,
    report_every: int = 25,
    runtime_dir: Path | None = None,
    max_projects: int = 0,
    max_sessions_per_call: int = DEFAULT_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL,
    max_session_chars: int = DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS,
    allow_empty_sessions: bool = DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS,
    extractor: Any | None = None,
    writer: Any | None = None,
) -> dict[str, Any]:
    lock_handle, locked_report = _acquire_runtime_lock(runtime_dir)
    if locked_report is not None:
        return {
            "schema_version": BULK_SEMANTIC_SCHEMA_VERSION,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "status": "already_running",
            "runtime_lock": locked_report.get("runtime_lock", {}),
            "mutation_performed": False,
            "raw_paths_printed": False,
        }

    started = time.monotonic()
    ledger = Ledger(ledger_path)
    artifact_store = LedgerSessionMemoryArtifactStore(ledger)
    projection_state_store = LedgerGraphProjectionStateStore(ledger)
    extractor = extractor or OpenAICompatibleBulkSemanticExtractor.from_env()
    writer = writer or DeterministicGraphitiSemanticWriter.from_env()

    eligible_sessions = _select_sessions(
        source_store,
        project=project,
        provider=provider,
        limit=0,
        projection_state_store=projection_state_store,
        extraction_level=EXTRACTION_LEVEL_ENTITY,
    )
    selected = eligible_sessions[: int(limit)] if limit > 0 else eligible_sessions
    total_available = _count_sessions(source_store, project=project, provider=provider)
    max_sessions_per_call = max(1, int(max_sessions_per_call))
    max_session_chars = max(200, int(max_session_chars))
    report_every = max(1, int(report_every))
    max_projects = max(0, int(max_projects))

    projected_cache: dict[str, dict[str, set[str]]] = {}
    by_provider: Counter[str] = Counter()
    by_project: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    durations: list[int] = []
    batch_items: list[tuple[dict[str, Any], Any]] = []
    projected = skipped_resumed = failed = materialized = llm_batches = 0
    entities_written = relations_written = 0
    fallback_single_session_batches = 0
    processed = 0
    stopped_after_max_projects = False

    _write_jsonl(
        progress_jsonl,
        {
            "event": "start",
            "selected": len(selected),
            "total_available": total_available,
            "limit": int(limit),
            "max_sessions_per_call": max_sessions_per_call,
            "max_session_chars": max_session_chars,
        },
    )

    try:
        for index, session in enumerate(selected, start=1):
            processed = index
            item_started = time.monotonic()
            session_project = str(session.get("project") or "")
            session_provider = str(session.get("provider") or "")
            session_id_hash = str(session.get("session_id_hash") or "")
            by_project[session_project] += 1
            by_provider[session_provider] += 1
            natural_id = session_id_hash.replace(":", "_")
            status = "unknown"
            reason = ""
            if max_projects and materialized >= max_projects:
                stopped_after_max_projects = True
                break
            try:
                if session_project not in projected_cache:
                    projected_cache[session_project] = (
                        projection_state_store.list_projected_source_hash_sets(
                            session_project,
                            extraction_level=EXTRACTION_LEVEL_ENTITY,
                            entity_type="Session",
                        )
                    )
                source_hash = str(session.get("source_hash") or "")
                if (
                    source_hash
                    and source_hash
                    in projected_cache[session_project].get(natural_id, set())
                ):
                    skipped_resumed += 1
                    status = "skipped_resumed"
                else:
                    episode = session_episode_from_couchdb_source(
                        session_id_hash=session_id_hash,
                        source_store=source_store,
                        artifact_store=artifact_store,
                        extractor_version="couchdb-bulk-semantic.1",
                    )
                    batch_items.append(
                        (
                            session,
                            make_bulk_session_input(
                                session_key=f"s{index}",
                                episode=episode,
                                max_chars=max_session_chars,
                            ),
                        )
                    )
                    materialized += 1
                    status = "queued"
                    if len(batch_items) >= max_sessions_per_call or (
                        max_projects and materialized >= max_projects
                    ):
                        report = _flush_batch_items(
                            batch_items,
                            extractor=extractor,
                            writer=writer,
                            projection_state_store=projection_state_store,
                            projected_cache=projected_cache,
                            dead_letter_jsonl=dead_letter_jsonl,
                            failure_reasons=failure_reasons,
                            allow_empty_sessions=allow_empty_sessions,
                        )
                        batch_items = []
                        llm_batches += int(report["llm_batches"])
                        fallback_single_session_batches += int(report["fallback_single_session_batches"])
                        projected += int(report["projected"])
                        failed += int(report["failed"])
                        entities_written += int(report["entities_written"])
                        relations_written += int(report["relations_written"])
                        if int(report["failed"]):
                            status = "failed"
                            reason = "extract_or_write"
                        else:
                            status = "projected"
            except Exception as exc:
                failed += 1
                status = "failed"
                reason = _reason_code(exc)
                failure_reasons[reason] += 1
                _write_dead_letter(
                    dead_letter_jsonl,
                    session=session,
                    reason_code=reason,
                    stage="materialize_extract_or_write",
                )

            elapsed_ms = int((time.monotonic() - item_started) * 1000)
            durations.append(elapsed_ms)
            if index == 1 or index % report_every == 0 or status == "failed" or index == len(selected):
                _write_jsonl(
                    progress_jsonl,
                    {
                        "event": "progress",
                        "index": index,
                        "selected": len(selected),
                        "status": status,
                        "reason_code": reason,
                        "elapsed_ms": elapsed_ms,
                        "project_ref": _project_ref(session_project),
                        "provider": session_provider,
                        "projected": projected,
                        "skipped_resumed": skipped_resumed,
                        "failed": failed,
                        "llm_batches": llm_batches,
                    },
                )
            if max_projects and materialized >= max_projects:
                stopped_after_max_projects = True
                break

        if batch_items and (not max_projects or projected < max_projects):
            report = _flush_batch_items(
                batch_items,
                extractor=extractor,
                writer=writer,
                projection_state_store=projection_state_store,
                projected_cache=projected_cache,
                dead_letter_jsonl=dead_letter_jsonl,
                failure_reasons=failure_reasons,
                allow_empty_sessions=allow_empty_sessions,
            )
            llm_batches += int(report["llm_batches"])
            fallback_single_session_batches += int(report["fallback_single_session_batches"])
            projected += int(report["projected"])
            failed += int(report["failed"])
            entities_written += int(report["entities_written"])
            relations_written += int(report["relations_written"])
    finally:
        if lock_handle is not None:
            _release_runtime_lock(lock_handle)

    elapsed_total_ms = int((time.monotonic() - started) * 1000)
    attempted = processed
    status = "ok" if failed == 0 else ("partial" if projected or skipped_resumed else "failed")
    return {
        "schema_version": BULK_SEMANTIC_SCHEMA_VERSION,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "status": status,
        "canonical_counts": {
            "source_sessions": total_available,
            "eligible_sessions": len(eligible_sessions),
            "selected_sessions": len(selected),
        },
        "filters": {
            "project_set": bool(project),
            "project_ref": _project_ref(project),
            "provider": provider,
        },
        "limit": int(limit),
        "truncated": bool(
            (limit > 0 and len(eligible_sessions) > len(selected))
            or stopped_after_max_projects
        ),
        "target_extraction_level": EXTRACTION_LEVEL_ENTITY,
        "runtime_lock": {
            "enabled": runtime_dir is not None,
            "acquired": runtime_dir is not None,
        },
        "projection": {
            "attempted": attempted,
            "materialized": materialized,
            "projected": projected,
            "skipped_resumed": skipped_resumed,
            "failed": failed,
            "failure_rate": (failed / attempted) if attempted else 0.0,
            "failure_reasons": dict(sorted(failure_reasons.items())),
            "stopped_after_max_projects": stopped_after_max_projects,
        },
        "semantic": {
            "llm_batches": llm_batches,
            "fallback_single_session_batches": fallback_single_session_batches,
            "max_sessions_per_call": max_sessions_per_call,
            "max_session_chars": max_session_chars,
            "entities_written": entities_written,
            "relations_written": relations_written,
            "allow_empty_sessions": bool(allow_empty_sessions),
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


def _flush_batch_items(
    batch_items: list[tuple[dict[str, Any], Any]],
    *,
    extractor: Any,
    writer: Any,
    projection_state_store: LedgerGraphProjectionStateStore,
    projected_cache: dict[str, dict[str, set[str]]],
    dead_letter_jsonl: Path | None,
    failure_reasons: Counter[str],
    allow_empty_sessions: bool,
) -> dict[str, int]:
    try:
        report = _extract_write_and_mark(
            batch_items,
            extractor=extractor,
            writer=writer,
            projection_state_store=projection_state_store,
            projected_cache=projected_cache,
            allow_empty_sessions=allow_empty_sessions,
        )
    except Exception as exc:
        reason = _reason_code(exc)
        if isinstance(exc, (json.JSONDecodeError, ValueError)) and len(batch_items) > 1:
            return _flush_batch_items_as_singletons(
                batch_items,
                extractor=extractor,
                writer=writer,
                projection_state_store=projection_state_store,
                projected_cache=projected_cache,
                dead_letter_jsonl=dead_letter_jsonl,
                failure_reasons=failure_reasons,
                allow_empty_sessions=allow_empty_sessions,
            )
        failure_reasons[reason] += len(batch_items)
        for session, _item in batch_items:
            _write_dead_letter(
                dead_letter_jsonl,
                session=session,
                reason_code=reason,
                stage="extract_or_write",
            )
        return {
            "projected": 0,
            "failed": len(batch_items),
            "entities_written": 0,
            "relations_written": 0,
            "llm_batches": 1,
            "fallback_single_session_batches": 0,
        }

    return {
        "projected": int(report.projected),
        "failed": 0,
        "entities_written": int(report.entities_written),
        "relations_written": int(report.relations_written),
        "llm_batches": 1,
        "fallback_single_session_batches": 0,
    }


def _flush_batch_items_as_singletons(
    batch_items: list[tuple[dict[str, Any], Any]],
    *,
    extractor: Any,
    writer: Any,
    projection_state_store: LedgerGraphProjectionStateStore,
    projected_cache: dict[str, dict[str, set[str]]],
    dead_letter_jsonl: Path | None,
    failure_reasons: Counter[str],
    allow_empty_sessions: bool,
) -> dict[str, int]:
    projected = failed = entities_written = relations_written = 0
    llm_batches = 1
    for batch_item in batch_items:
        try:
            report = _extract_write_and_mark(
                [batch_item],
                extractor=extractor,
                writer=writer,
                projection_state_store=projection_state_store,
                projected_cache=projected_cache,
                allow_empty_sessions=allow_empty_sessions,
            )
        except Exception as exc:
            reason = _reason_code(exc)
            failure_reasons[reason] += 1
            session, _item = batch_item
            _write_dead_letter(
                dead_letter_jsonl,
                session=session,
                reason_code=reason,
                stage="extract_or_write_singleton_fallback",
            )
            failed += 1
        else:
            projected += int(report.projected)
            entities_written += int(report.entities_written)
            relations_written += int(report.relations_written)
        llm_batches += 1
    return {
        "projected": projected,
        "failed": failed,
        "entities_written": entities_written,
        "relations_written": relations_written,
        "llm_batches": llm_batches,
        "fallback_single_session_batches": len(batch_items),
    }


def _extract_write_and_mark(
    batch_items: list[tuple[dict[str, Any], Any]],
    *,
    extractor: Any,
    writer: Any,
    projection_state_store: LedgerGraphProjectionStateStore,
    projected_cache: dict[str, dict[str, set[str]]],
    allow_empty_sessions: bool,
) -> Any:
    batch_inputs = [item for _session, item in batch_items]
    extraction = extractor.extract(list(batch_inputs))
    report = writer.write_batch(
        list(batch_inputs),
        extraction,
        allow_empty_sessions=allow_empty_sessions,
    )
    written_natural_ids = set(report.projected_natural_ids)
    for item in batch_inputs:
        episode = item.episode
        if episode.natural_id not in written_natural_ids:
            continue
        projection_state_store.mark_projected(
            episode,
            "inserted",
            extraction_level=EXTRACTION_LEVEL_ENTITY,
        )
        project = str(episode.payload.get("project") or "")
        projected_cache.setdefault(project, {}).setdefault(
            episode.natural_id, set()
        ).add(str(episode.payload.get("source_hash") or ""))
    return report


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return int(ordered[index])


def _positive_int_env(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _reason_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code is None:
        return type(exc).__name__
    try:
        return f"{type(exc).__name__}_{int(code)}"
    except (TypeError, ValueError):
        return type(exc).__name__
