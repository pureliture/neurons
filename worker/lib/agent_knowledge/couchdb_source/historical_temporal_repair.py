"""Repair historical CouchDB temporal gaps from re-parsed provider evidence.

This command is intentionally narrower than transcript migration: it never
calls ``put`` for transcript/session/chunk imports.  It reads a bounded live
snapshot of temporal-gap conversation chunks, re-parses the selected provider
sources, and conditionally patches only a deterministic document id whose
content hash *and* CouchDB revision still match that snapshot.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .couchdb_http_store import CouchDBHttpSourceStore
from .document_model import (
    SourceDocType,
    build_conversation_chunk_document,
    build_source_locator_hash,
    normalize_observed_interval,
    sha256_hash,
)
from .migration_cli import (
    MIGRATION_PROVIDERS,
    _gemini_project_from_path,
    _grok_project_from_path,
    convert_gemini_json_to_fixture,
    enumerate_provider_files,
    extract_cwd,
)
from .project_authority import ProjectAuthorityInput, resolve_project
from .session_memory_materializer import (
    mark_projection_pending_if_source_changed,
    update_coverage_with_tool_evidence,
)
from .source_store import CouchDBSourceStore, SourceStoreConflict
from ..session_memory.native_memory_sync_approval import (
    ApprovalError,
    validate_memory_enqueue_approval,
)
from ..session_memory.transcript_chunking import build_transcript_chunks
from ..session_memory.transcript_model import canonicalize_provider
from ..session_memory.transcript_parsers import parse_transcript_source


REPAIR_OPERATION = "couchdb_historical_temporal_repair"
REPAIR_SCHEMA_VERSION = "couchdb_historical_temporal_repair.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SNAPSHOT_FIELDS = [
    "_id",
    "_rev",
    "session_id_hash",
    "provider",
    "project",
    "content_hash",
    "observed_at_start",
    "observed_at_end",
]


@dataclass(frozen=True)
class _Target:
    couchdb_url: str = field(repr=False)
    couchdb_db: str = field(repr=False)
    couchdb_user: str = field(repr=False)
    couchdb_password: str = field(repr=False)
    target_fingerprints: dict[str, str]


@dataclass(frozen=True)
class _PlanItem:
    document_id: str
    expected_rev: str
    expected_content_hash: str
    session_id_hash: str
    provider: str
    project: str
    observed_at_start: str
    observed_at_end: str

    @property
    def digest(self) -> str:
        return sha256_hash(
            json.dumps(
                {
                    "document_ref_hash": sha256_hash(self.document_id),
                    "revision_hash": sha256_hash(self.expected_rev),
                    "content_hash": self.expected_content_hash,
                    "observed_at_start_hash": sha256_hash(self.observed_at_start),
                    "observed_at_end_hash": sha256_hash(self.observed_at_end),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def _target_fingerprint(value: Mapping[str, object]) -> str:
    return sha256_hash(json.dumps(dict(value), sort_keys=True, separators=(",", ":")))


def _resolve_target(environ: Mapping[str, str]) -> _Target:
    url = str(environ.get("COUCHDB_URL") or "").strip().rstrip("/")
    database = str(environ.get("COUCHDB_DB") or "neurons_transcript_source").strip()
    return _Target(
        couchdb_url=url,
        couchdb_db=database,
        couchdb_user=str(environ.get("COUCHDB_USER") or "").strip(),
        couchdb_password=str(environ.get("COUCHDB_PASSWORD") or ""),
        target_fingerprints={
            "couchdb_source": _target_fingerprint(
                {"kind": "couchdb_source", "base_url": url, "database": database}
            )
        },
    )


def _auth_header(user: str, password: str) -> str:
    if not user:
        return ""
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _validate_bounds(
    *,
    provider: str,
    project: str,
    source_file_limit: int,
    target_document_limit: int,
    patch_limit: int,
    max_runtime_seconds: float,
) -> None:
    if canonicalize_provider(provider) not in MIGRATION_PROVIDERS:
        raise ValueError("provider scope is invalid")
    if not str(project or "").strip():
        raise ValueError("project scope is required")
    if any(int(limit) <= 0 for limit in (source_file_limit, target_document_limit, patch_limit)):
        raise ValueError("all limits must be positive")
    if not math.isfinite(float(max_runtime_seconds)) or float(max_runtime_seconds) <= 0:
        raise ValueError("max_runtime_seconds must be positive")


def _gap_kind(document: Mapping[str, object]) -> str:
    start = str(document.get("observed_at_start") or "")
    end = str(document.get("observed_at_end") or "")
    if not start and not end:
        return "absent"
    if normalize_observed_interval(start, end) is None:
        return "malformed"
    return "complete"


def _snapshot_gaps(
    store: CouchDBSourceStore,
    *,
    provider: str,
    project: str,
    target_document_limit: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, int]]:
    documents = store.find_by_type(
        SourceDocType.CONVERSATION_CHUNK,
        fields=_SNAPSHOT_FIELDS,
        selector={"project": project, "provider": provider},
        limit=int(target_document_limit) + 1,
    )
    if len(documents) > int(target_document_limit):
        raise ValueError("target_document_limit_exceeded")
    snapshot = [dict(document) for document in documents]
    gaps = [document for document in snapshot if _gap_kind(document) != "complete"]
    return snapshot, gaps, {
        "snapshot_document_count": len(documents),
        "snapshot_gap_count": len(gaps),
        "snapshot_complete_count": len(documents) - len(gaps),
    }


def _source_project(provider: str, path: Path) -> str:
    cwd = extract_cwd(provider, path)
    capture_project = cwd
    if not capture_project:
        if provider == "antigravity":
            capture_project = "antigravity"
        elif provider == "gemini":
            capture_project = _gemini_project_from_path(path)
        elif provider == "grok":
            capture_project = _grok_project_from_path(path)
    provider_path = "" if provider == "grok" else str(path)
    return resolve_project(
        ProjectAuthorityInput(
            capture_metadata_project=capture_project,
            provider_source_path=provider_path,
            cwd=cwd,
        )
    ).project


def collect_historical_candidates(
    *,
    provider: str,
    project: str,
    source_root: Path,
    source_file_limit: int,
    monotonic: Callable[[], float] = time.monotonic,
    started: float | None = None,
    max_runtime_seconds: float | None = None,
) -> tuple[list[dict[str, object]], dict[str, int], bool]:
    """Re-parse bounded historical sources without invoking migration/upsert."""
    discovered = enumerate_provider_files(provider, source_root)[: int(source_file_limit) + 1]
    source_file_limit_exceeded = len(discovered) > int(source_file_limit)
    files = discovered[: int(source_file_limit)]
    documents: list[dict[str, object]] = []
    parsed_source_count = 0
    parser_error_count = 0
    excluded_temporal_count = 0
    timed_out = False
    with tempfile.TemporaryDirectory(prefix="neurons-historical-temporal-repair-") as temporary:
        runtime_dir = Path(temporary)
        for path in files:
            if _deadline_exceeded(started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic):
                timed_out = True
                break
            try:
                resolved_project = _source_project(provider, path)
                if resolved_project != project:
                    continue
                parser_path = path
                # Native Gemini .json chats are not parser fixtures. Convert in a
                # private temporary directory, but keep the original locator hash
                # so reconstructed document IDs match the historical source.
                if provider == "gemini" and path.suffix.lower() == ".json":
                    parser_path = convert_gemini_json_to_fixture(path, runtime_dir)
                source_locator_hash = build_source_locator_hash(str(path))
                parsed = parse_transcript_source(
                    provider,
                    parser_path,
                    project=resolved_project,
                    source_locator_hash=source_locator_hash,
                )
                parsed_source_count += 1
                for chunk in build_transcript_chunks(parsed):
                    if _deadline_exceeded(started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic):
                        timed_out = True
                        break
                    document = build_conversation_chunk_document(
                        chunk=chunk,
                        source_locator_hash=source_locator_hash,
                    )
                    if _provider_native_interval(
                        document.get("observed_at_start"), document.get("observed_at_end")
                    ) is None:
                        excluded_temporal_count += 1
                        continue
                    documents.append(document)
                if timed_out:
                    break
            except Exception:
                parser_error_count += 1
    return documents, {
        "source_file_count": len(files),
        "source_file_limit_exceeded": source_file_limit_exceeded,
        "source_file_truncated": source_file_limit_exceeded,
        "parsed_source_count": parsed_source_count,
        "parser_error_count": parser_error_count,
        "excluded_temporal_candidate_count": excluded_temporal_count,
    }, timed_out


def _deadline_exceeded(
    *,
    started: float | None,
    max_runtime_seconds: float | None,
    monotonic: Callable[[], float],
) -> bool:
    return (
        started is not None
        and max_runtime_seconds is not None
        and monotonic() >= started + max_runtime_seconds
    )


def _provider_native_interval(start: object, end: object) -> tuple[str, str] | None:
    """Validate provider evidence without accepting inferred/naive values."""
    raw_start = str(start or "").strip()
    raw_end = str(end or "").strip()
    if not raw_start or not raw_end:
        return None
    try:
        from datetime import datetime, timezone

        parsed_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_start.tzinfo is None or parsed_end.tzinfo is None:
        return None
    normalized_start = parsed_start.astimezone(timezone.utc)
    normalized_end = parsed_end.astimezone(timezone.utc)
    if normalized_start > normalized_end:
        return None
    return (
        normalized_start.isoformat().replace("+00:00", "Z"),
        normalized_end.isoformat().replace("+00:00", "Z"),
    )


def _snapshot_digest_entry(document: Mapping[str, object]) -> dict[str, str]:
    return {
        "document_ref_hash": sha256_hash(str(document.get("_id") or "")),
        "revision_hash": sha256_hash(str(document.get("_rev") or "")),
        "content_hash": str(document.get("content_hash") or ""),
        "observed_at_start": str(document.get("observed_at_start") or ""),
        "observed_at_end": str(document.get("observed_at_end") or ""),
    }


def _plan_digest(
    items: Iterable[_PlanItem],
    *,
    provider: str,
    project: str,
    snapshot_documents: Iterable[Mapping[str, object]],
    target_fingerprints: Mapping[str, str],
    source_file_limit: int,
    target_document_limit: int,
    patch_limit: int,
    max_runtime_seconds: float,
) -> str:
    snapshot = list(snapshot_documents)
    return sha256_hash(
        json.dumps(
            {
                "scope": {"provider": provider, "project": project},
                "bounds": {
                    "source_file_limit": int(source_file_limit),
                    "target_document_limit": int(target_document_limit),
                    "patch_limit": int(patch_limit),
                    "max_runtime_seconds": float(max_runtime_seconds),
                },
                "items": sorted(item.digest for item in items),
                "snapshot_document_count": len(snapshot),
                "snapshot_documents": sorted(
                    (_snapshot_digest_entry(document) for document in snapshot),
                    key=lambda entry: entry["document_ref_hash"],
                ),
                "target_fingerprints": dict(sorted(target_fingerprints.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _revision_generation(value: object) -> int | None:
    generation, separator, digest = str(value or "").partition("-")
    if separator != "-" or not digest or not generation.isdigit():
        return None
    parsed = int(generation)
    return parsed if parsed > 0 else None


def _uncertain_temporal_patch_is_applied(
    current: Mapping[str, object],
    *,
    item: _PlanItem,
) -> bool:
    expected_generation = _revision_generation(item.expected_rev)
    current_generation = _revision_generation(current.get("_rev"))
    return (
        str(current.get("_id") or "") == item.document_id
        and str(current.get("content_hash") or "") == item.expected_content_hash
        and str(current.get("session_id_hash") or "") == item.session_id_hash
        and str(current.get("provider") or "") == item.provider
        and str(current.get("project") or "") == item.project
        and expected_generation is not None
        and current_generation is not None
        and current_generation > expected_generation
        and normalize_observed_interval(
            current.get("observed_at_start"), current.get("observed_at_end")
        )
        == (item.observed_at_start, item.observed_at_end)
    )


def repair_historical_temporal_gaps(
    *,
    source_store: CouchDBSourceStore,
    provider: str,
    project: str,
    historical_documents: Iterable[Mapping[str, object]],
    source_file_limit: int,
    target_document_limit: int,
    patch_limit: int,
    max_runtime_seconds: float,
    execute: bool = False,
    expected_plan_digest: str = "",
    target_fingerprints: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    started: float | None = None,
) -> dict[str, Any]:
    """Snapshot, plan, and conditionally patch historical temporal gaps.

    ``historical_documents`` must be built by the provider parser.  No migration
    upsert seam is accepted here; only ``patch_observed_time_if_content_hash`` is
    used for source writes.
    """
    provider = canonicalize_provider(provider)
    project = str(project or "").strip()
    _validate_bounds(
        provider=provider,
        project=project,
        source_file_limit=source_file_limit,
        target_document_limit=target_document_limit,
        patch_limit=patch_limit,
        max_runtime_seconds=max_runtime_seconds,
    )
    started = monotonic() if started is None else started
    target = dict(sorted((target_fingerprints or {}).items()))
    report: dict[str, Any] = {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "status": "dry_run" if not execute else "completed",
        "dry_run": not execute,
        "provider": provider,
        "project_scope_hash": sha256_hash(project),
        "source_file_limit": int(source_file_limit),
        "target_document_limit": int(target_document_limit),
        "patch_limit": int(patch_limit),
        "max_runtime_seconds": float(max_runtime_seconds),
        "snapshot_document_count": 0,
        "snapshot_gap_count": 0,
        "snapshot_complete_count": 0,
        "historical_candidate_count": 0,
        "candidate_duplicate_count": 0,
        "archive_conflict_count": 0,
        "target_archive_conflict_count": 0,
        "planned_update_count": 0,
        "content_conflict_count": 0,
        "snapshot_integrity_error_count": 0,
        "write_conflict_count": 0,
        "write_error_count": 0,
        "updated_count": 0,
        "coverage_recomputed_session_count": 0,
        "projection_pending_session_count": 0,
        "partial_session_count": 0,
        "partial_session_gap_count": 0,
        "postcheck_snapshot_document_count": 0,
        "postcheck_gap_count": 0,
        "remaining_temporal_gap_count": 0,
        "plan_digest": "",
        "expected_plan_digest_match": None,
        "timed_out": False,
        "mutation_performed": False,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
        "target_fingerprints": target,
    }
    if _deadline_exceeded(
        started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic
    ):
        report.update(
            {"status": "aborted_timeout", "timed_out": True, "error_count": 1, "gap_count": 1}
        )
        return report
    try:
        snapshot_documents, gaps, snapshot_counts = _snapshot_gaps(
            source_store,
            provider=provider,
            project=project,
            target_document_limit=target_document_limit,
        )
    except ValueError as exc:
        report.update({"status": "blocked", "error": str(exc), "error_count": 1, "gap_count": 1})
        return report
    except Exception:
        report.update(
            {
                "status": "blocked",
                "error": "snapshot_read_failed",
                "error_count": 1,
                "gap_count": 1,
            }
        )
        return report
    report.update(snapshot_counts)
    if _deadline_exceeded(
        started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic
    ):
        report.update(
            {
                "status": "aborted_timeout",
                "timed_out": True,
                "error_count": 1,
                "gap_count": max(1, len(gaps)),
            }
        )
        return report
    target_gap_ids = {str(document.get("_id") or "") for document in gaps}
    by_id: dict[str, dict[str, object]] = {}
    archive_conflicts: set[str] = set()
    for document in historical_documents:
        if str(document.get("provider") or "") != provider or str(document.get("project") or "") != project:
            continue
        bounds = _provider_native_interval(
            document.get("observed_at_start"), document.get("observed_at_end")
        )
        if bounds is None:
            continue
        document_id = str(document.get("_id") or "")
        if not document_id or document_id in archive_conflicts:
            continue
        candidate = dict(document)
        candidate["observed_at_start"], candidate["observed_at_end"] = bounds
        existing = by_id.get(document_id)
        if existing is None:
            by_id[document_id] = candidate
        elif (
            str(existing.get("content_hash") or "") == str(candidate.get("content_hash") or "")
            and str(existing.get("session_id_hash") or "") == str(candidate.get("session_id_hash") or "")
            and str(existing.get("observed_at_start") or "") == str(candidate.get("observed_at_start") or "")
            and str(existing.get("observed_at_end") or "") == str(candidate.get("observed_at_end") or "")
        ):
            report["candidate_duplicate_count"] += 1
        else:
            by_id.pop(document_id, None)
            archive_conflicts.add(document_id)
            report["archive_conflict_count"] += 1
    report["historical_candidate_count"] = len(by_id)
    report["target_archive_conflict_count"] = len(archive_conflicts & target_gap_ids)
    planned: list[_PlanItem] = []
    for snapshot in gaps:
        if _deadline_exceeded(
            started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic
        ):
            report["timed_out"] = True
            break
        document_id = str(snapshot.get("_id") or "")
        candidate = by_id.get(document_id)
        expected_rev = str(snapshot.get("_rev") or "")
        expected_hash = str(snapshot.get("content_hash") or "")
        if not document_id or not expected_rev or not _SHA256_RE.fullmatch(expected_hash):
            report["snapshot_integrity_error_count"] += 1
            continue
        if document_id in archive_conflicts:
            continue
        if candidate is None or str(candidate.get("content_hash") or "") != expected_hash:
            report["content_conflict_count"] += 1
            continue
        bounds = _provider_native_interval(candidate.get("observed_at_start"), candidate.get("observed_at_end"))
        if bounds is None:
            report["snapshot_integrity_error_count"] += 1
            continue
        planned.append(
            _PlanItem(
                document_id=document_id,
                expected_rev=expected_rev,
                expected_content_hash=expected_hash,
                session_id_hash=str(candidate.get("session_id_hash") or ""),
                provider=provider,
                project=project,
                observed_at_start=bounds[0],
                observed_at_end=bounds[1],
            )
        )
    if report["timed_out"]:
        report.update(
            {
                "status": "aborted_timeout",
                "error_count": 1,
                "gap_count": max(1, len(gaps) - len(planned)),
            }
        )
        return report
    report["planned_update_count"] = len(planned)
    report["plan_digest"] = _plan_digest(
        planned,
        provider=provider,
        project=project,
        snapshot_documents=snapshot_documents,
        target_fingerprints=target,
        source_file_limit=source_file_limit,
        target_document_limit=target_document_limit,
        patch_limit=patch_limit,
        max_runtime_seconds=max_runtime_seconds,
    )
    if execute and expected_plan_digest != report["plan_digest"]:
        report.update({"status": "blocked_plan_drift", "expected_plan_digest_match": False, "error_count": 1, "gap_count": 1})
        return report
    if len(planned) > patch_limit:
        report.update(
            {
                "status": "blocked_patch_limit" if execute else "dry_run_patch_limit_exceeded",
                "error": "patch_limit_exceeded",
                "error_count": 1,
                "gap_count": 1,
            }
        )
        return report
    if execute:
        report["expected_plan_digest_match"] = True
        failed_sessions: set[str] = set()
        mutated_sessions: set[str] = set()
        for item in planned:
            if _deadline_exceeded(
                started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic
            ):
                report["timed_out"] = True
                failed_sessions.add(item.session_id_hash)
                break
            try:
                try:
                    revision = source_store.patch_observed_time_if_content_hash(
                        doc_id=item.document_id,
                        expected_content_hash=item.expected_content_hash,
                        expected_rev=item.expected_rev,
                        observed_at_start=item.observed_at_start,
                        observed_at_end=item.observed_at_end,
                    )
                except SourceStoreConflict:
                    raise
                except Exception:
                    current = source_store.get(item.document_id) or {}
                    if not _uncertain_temporal_patch_is_applied(current, item=item):
                        raise
                    patch_mutated = True
                else:
                    patch_mutated = revision.outcome != "duplicate"
                    current = source_store.get(item.document_id) or {}
                if patch_mutated:
                    report["updated_count"] += 1
                    report["mutation_performed"] = True
                    mutated_sessions.add(item.session_id_hash)
                if (
                    str(current.get("content_hash") or "") != item.expected_content_hash
                    or normalize_observed_interval(
                        current.get("observed_at_start"), current.get("observed_at_end")
                    ) != (item.observed_at_start, item.observed_at_end)
                ):
                    raise SourceStoreConflict("historical temporal repair postcheck failed")
            except SourceStoreConflict:
                report["write_conflict_count"] += 1
                failed_sessions.add(item.session_id_hash)
            except Exception:
                report["write_error_count"] += 1
                failed_sessions.add(item.session_id_hash)
        partial_sessions = mutated_sessions & failed_sessions
        report["partial_session_count"] = len(partial_sessions)
        report["partial_session_gap_count"] = len(partial_sessions)
        # A session changed by an earlier CAS must have its derived state
        # refreshed even when another item in that session later conflicts.
        for session_id_hash in sorted(mutated_sessions):
            try:
                coverage = update_coverage_with_tool_evidence(
                    session_id_hash=session_id_hash, store=source_store
                )
                if coverage is None or not str(coverage.get("source_hash") or ""):
                    raise ValueError("coverage source hash is unavailable")
                mark_projection_pending_if_source_changed(
                    session_id_hash=session_id_hash,
                    provider=provider,
                    project=project,
                    source_hash=str(coverage["source_hash"]),
                    store=source_store,
                    source_changed=True,
                )
                report["coverage_recomputed_session_count"] += 1
                report["projection_pending_session_count"] += 1
            except Exception:
                report["write_error_count"] += 1
        if report["timed_out"]:
            report["status"] = "aborted_timeout"
        elif _deadline_exceeded(
            started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic
        ):
            report["timed_out"] = True
            report["status"] = "aborted_timeout"
        else:
            try:
                _, remaining, postcheck_counts = _snapshot_gaps(
                    source_store,
                    provider=provider,
                    project=project,
                    target_document_limit=target_document_limit,
                )
                report["postcheck_snapshot_document_count"] = postcheck_counts["snapshot_document_count"]
                report["postcheck_gap_count"] = postcheck_counts["snapshot_gap_count"]
                report["remaining_temporal_gap_count"] = len(remaining)
            except Exception:
                report["write_error_count"] += 1
    report["gap_count"] = (
        int(report["content_conflict_count"])
        + int(report["snapshot_integrity_error_count"])
        + int(report["write_conflict_count"])
        + int(report["write_error_count"])
        + int(report["target_archive_conflict_count"])
        + int(report["remaining_temporal_gap_count"])
    )
    report["error_count"] = report["gap_count"]
    if report["error_count"] and not report["timed_out"]:
        report["status"] = "dry_run_with_gaps" if not execute else "completed_with_errors"
    return report


def _error_report(error: str, *, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "status": "blocked",
        "error": error,
        "dry_run": dry_run,
        "mutation_performed": False,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-historical-temporal-repair")
    parser.add_argument("--provider", required=True, choices=list(MIGRATION_PROVIDERS))
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--source-file-limit", required=True, type=int)
    parser.add_argument("--target-document-limit", required=True, type=int)
    parser.add_argument("--patch-limit", required=True, type=int)
    parser.add_argument("--max-runtime-seconds", required=True, type=float)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-digest", default="")
    parser.add_argument("--approval", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    execute = bool(args.execute)
    try:
        _validate_bounds(
            provider=args.provider,
            project=args.project,
            source_file_limit=args.source_file_limit,
            target_document_limit=args.target_document_limit,
            patch_limit=args.patch_limit,
            max_runtime_seconds=args.max_runtime_seconds,
        )
    except ValueError:
        print(json.dumps(_error_report("invalid_bounds", dry_run=not execute), sort_keys=True))
        return 2
    target = _resolve_target(os.environ)
    if not target.couchdb_url:
        print(json.dumps(_error_report("env_missing", dry_run=not execute), sort_keys=True))
        return 2
    if execute:
        if _SHA256_RE.fullmatch(str(args.expected_plan_digest or "")) is None:
            print(json.dumps(_error_report("expected_plan_digest_invalid", dry_run=False), sort_keys=True))
            return 2
        try:
            approval = validate_memory_enqueue_approval(
                args.approval or None, operation=REPAIR_OPERATION, command_argv=effective_argv
            )
            approved = (approval.get("target") or {}).get("target_fingerprints")
            if approved != target.target_fingerprints:
                raise ApprovalError("approval target fingerprint mismatch")
            if float(approval.get("timeout_seconds") or 0) < float(args.max_runtime_seconds):
                raise ApprovalError("approval timeout is below execution bound")
        except ApprovalError:
            print(json.dumps(_error_report("approval_rejected", dry_run=False), sort_keys=True))
            return 2
    started = time.monotonic()
    candidates, collection, timed_out = collect_historical_candidates(
        provider=canonicalize_provider(args.provider),
        project=str(args.project),
        source_root=Path(args.source_root).expanduser(),
        source_file_limit=int(args.source_file_limit),
        started=started,
        max_runtime_seconds=float(args.max_runtime_seconds),
    )
    if timed_out or collection["source_file_limit_exceeded"]:
        report = _error_report(
            "runtime_bound_exceeded" if timed_out else "source_file_limit_exceeded",
            dry_run=not execute,
        )
        report.update(collection)
        print(json.dumps(report, sort_keys=True))
        return 1
    if collection["parser_error_count"]:
        report = _error_report("historical_source_parse_error", dry_run=not execute)
        report.update(collection)
        report.update(
            {
                "error_count": int(collection["parser_error_count"]),
                "gap_count": int(collection["parser_error_count"]),
            }
        )
        print(json.dumps(report, sort_keys=True))
        return 1
    store = CouchDBHttpSourceStore(
        base_url=target.couchdb_url,
        db=target.couchdb_db,
        auth_header=_auth_header(target.couchdb_user, target.couchdb_password),
        request_timeout_seconds=min(30.0, max(0.001, float(args.max_runtime_seconds) - (time.monotonic() - started))),
        deadline_monotonic=started + float(args.max_runtime_seconds),
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=args.provider,
        project=args.project,
        historical_documents=candidates,
        source_file_limit=int(args.source_file_limit),
        target_document_limit=int(args.target_document_limit),
        patch_limit=int(args.patch_limit),
        max_runtime_seconds=float(args.max_runtime_seconds),
        execute=execute,
        expected_plan_digest=str(args.expected_plan_digest or ""),
        target_fingerprints=target.target_fingerprints,
        started=started,
    )
    report.update(collection)
    print(json.dumps(report, sort_keys=True))
    return 0 if not report.get("error_count") and not report.get("timed_out") else 1


__all__ = [
    "REPAIR_OPERATION",
    "REPAIR_SCHEMA_VERSION",
    "collect_historical_candidates",
    "repair_historical_temporal_gaps",
    "main",
]
