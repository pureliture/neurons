"""Bounded recovery of missing CouchDB temporal metadata from ingress state.

``delivery_payloads`` persists the already-redacted wire request and is the
authoritative recovery source for metadata that an older CouchDB delivery path
failed to preserve.  This module never emits source identifiers or bodies.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import math
import os
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Callable

from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from ..couchdb_source.document_model import (
    conversation_chunk_doc_id,
    coverage_manifest_doc_id,
    projection_state_doc_id,
    sha256_hash,
)
from ..couchdb_source.session_memory_materializer import (
    _coverage_snapshot,
    mark_projection_pending_if_source_changed,
    update_coverage_with_tool_evidence,
)
from ..couchdb_source.source_store import CouchDBSourceStore, SourceStoreConflict
from ..session_memory.native_memory_sync_approval import (
    ApprovalError,
    validate_memory_enqueue_approval,
)
from .delivery_backend import PAYLOAD_OK, resolve_delivery_payload
from .server_runtime import apply_server_redaction
from .state_db import RAGIngressStateDB
from .state_cli import DEFAULT_TRANSCRIPT_TARGET_PROFILE


BACKFILL_OPERATION = "couchdb_temporal_metadata_backfill"
BACKFILL_SCHEMA_VERSION = "couchdb_temporal_metadata_backfill.v1"


@dataclass(frozen=True)
class _Candidate:
    session_id_hash: str
    chunk_id: str
    provider: str
    project: str
    observed_at_start: str
    observed_at_end: str
    expected_source_content_hash: str

    @property
    def document_id(self) -> str:
        return conversation_chunk_doc_id(self.session_id_hash, self.chunk_id)

    @property
    def digest(self) -> str:
        payload = {
            "document_ref_hash": sha256_hash(self.document_id),
            "observed_at_start_hash": sha256_hash(self.observed_at_start),
            "observed_at_end_hash": sha256_hash(self.observed_at_end),
            "source_content_hash": self.expected_source_content_hash,
        }
        return sha256_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _validate_bounds(*, project: str, limit: int, max_runtime_seconds: float) -> None:
    if not str(project or "").strip():
        raise ValueError("project scope is required")
    if int(limit) <= 0:
        raise ValueError("limit must be positive")
    if not math.isfinite(float(max_runtime_seconds)) or float(max_runtime_seconds) <= 0:
        raise ValueError("max_runtime_seconds must be positive")


def _payload_rows(
    state_db: RAGIngressStateDB,
    *,
    project: str,
    page_size: int = 100,
) -> Iterator[dict[str, str]]:
    """Stream project-scoped payload rows in deterministic bounded pages.

    Raw keys stay inside this private function and are never copied into the
    public report.  The caller applies the runtime bound and mutation limit.
    """

    with state_db.connect() as connection:
        cursor = connection.execute(
            """
            SELECT
                payload.idempotency_key AS idempotency_key,
                payload.payload_hash AS payload_hash,
                COALESCE(job.status, '') AS delivery_status,
                COALESCE(job.payload_hash, '') AS delivery_payload_hash,
                COALESCE(job.target_profile, '') AS delivery_target_profile,
                COALESCE(job.document_kind, '') AS delivery_document_kind
            FROM delivery_payloads AS payload
            LEFT JOIN delivery_jobs AS job
              ON job.idempotency_key = payload.idempotency_key
            WHERE json_valid(payload.payload_json)
              AND COALESCE(
                    NULLIF(json_extract(payload.payload_json, '$.payload.document.metadata.project'), ''),
                    json_extract(payload.payload_json, '$.source.project'),
                    ''
                  ) = ?
              AND COALESCE(
                    NULLIF(json_extract(payload.payload_json, '$.payload.document.metadata.type'), ''),
                    json_extract(payload.payload_json, '$.kind'),
                    ''
                  ) = 'conversation_chunk'
            ORDER BY payload.recorded_at ASC, payload.idempotency_key ASC
            """,
            (project,),
        )
        while True:
            rows = cursor.fetchmany(max(1, int(page_size)))
            if not rows:
                return
            for row in rows:
                yield {
                    "idempotency_key": str(row["idempotency_key"] or ""),
                    "payload_hash": str(row["payload_hash"] or ""),
                    "delivery_status": str(row["delivery_status"] or ""),
                    "delivery_payload_hash": str(
                        row["delivery_payload_hash"] or ""
                    ),
                    "delivery_target_profile": str(
                        row["delivery_target_profile"] or ""
                    ),
                    "delivery_document_kind": str(
                        row["delivery_document_kind"] or ""
                    ),
                }


def _aware_iso8601(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("temporal metadata is missing")
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("temporal metadata is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("temporal metadata must include timezone")
    return text


def _candidate_from_payload(payload: dict, *, expected_project: str) -> _Candidate:
    package = payload.get("payload") or {}
    document = package.get("document") or {}
    metadata = document.get("metadata") or {}
    source = payload.get("source") or {}
    if not isinstance(metadata, dict) or not isinstance(source, dict):
        raise ValueError("wire metadata shape is invalid")
    project = str(metadata.get("project") or source.get("project") or "")
    if project != expected_project:
        raise ValueError("wire project scope mismatch")
    session_id_hash = str(metadata.get("session_id_hash") or "")
    chunk_id = str(metadata.get("chunk_id") or "")
    if not session_id_hash or not chunk_id:
        raise ValueError("wire source identity metadata is missing")
    observed_at_start = _aware_iso8601(metadata.get("observed_at_start"))
    observed_at_end = _aware_iso8601(metadata.get("observed_at_end"))
    start = datetime.datetime.fromisoformat(observed_at_start.replace("Z", "+00:00"))
    end = datetime.datetime.fromisoformat(observed_at_end.replace("Z", "+00:00"))
    if end < start:
        raise ValueError("wire temporal range is invalid")
    redacted_payload = apply_server_redaction(payload)
    redacted_body = str(
        (((redacted_payload.get("payload") or {}).get("document") or {}).get("body"))
        or ""
    )
    return _Candidate(
        session_id_hash=session_id_hash,
        chunk_id=chunk_id,
        provider=str(metadata.get("provider") or source.get("provider") or "ingress"),
        project=project,
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        expected_source_content_hash=sha256_hash(redacted_body),
    )


def _plan_digest(planned: list[tuple[_Candidate, dict]]) -> str:
    """Bind approval to the exact bounded source revisions selected for writes."""

    items = [
        json.dumps(
            {
                "candidate_digest": candidate.digest,
                "source_revision_hash": sha256_hash(str(existing.get("_rev") or "")),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for candidate, existing in planned
    ]
    return sha256_hash("\n".join(sorted(items)))


def _aggregate_is_current(
    *,
    source_store: CouchDBSourceStore,
    session_id_hash: str,
) -> bool:
    """Pure currentness check used to distinguish a true duplicate from repair.

    A prior attempt can persist the chunk metadata and fail before refreshing
    coverage/session/projection state.  Such a retry is not an exact duplicate:
    it must resume aggregate reconciliation without rewriting the chunk.
    """

    snapshot = _coverage_snapshot(session_id_hash=session_id_hash, store=source_store)
    if snapshot is None:
        return False
    expected, sessions, expected_start, expected_end = snapshot
    persisted = source_store.get(coverage_manifest_doc_id(session_id_hash)) or {}
    projection = source_store.get(projection_state_doc_id(session_id_hash)) or {}
    session = sessions[0] if sessions else {}
    expected_hash = str(expected.get("source_hash") or "")
    if not expected_hash:
        return False
    return (
        str(persisted.get("source_hash") or "") == expected_hash
        and int(persisted.get("conversation_chunk_count") or 0)
        == int(expected.get("conversation_chunk_count") or 0)
        and int(persisted.get("tool_evidence_bundle_count") or 0)
        == int(expected.get("tool_evidence_bundle_count") or 0)
        and str(persisted.get("observed_at_start") or "") == expected_start
        and str(persisted.get("observed_at_end") or "") == expected_end
        and str(session.get("source_hash") or "") == expected_hash
        and str(session.get("observed_at_start") or "") == expected_start
        and str(session.get("observed_at_end") or "") == expected_end
        and str(projection.get("source_hash") or "") == expected_hash
    )


def _chunk_revision_matches(
    document: dict,
    *,
    candidate: _Candidate,
    expected_rev: str,
    expected_body: object,
    expected_content_hash: object,
) -> bool:
    return (
        document.get("body") == expected_body
        and document.get("content_hash") == expected_content_hash
        and str(document.get("_rev") or "") == str(expected_rev or "")
        and str(document.get("observed_at_start") or "") == candidate.observed_at_start
        and str(document.get("observed_at_end") or "") == candidate.observed_at_end
    )


def backfill_temporal_metadata(
    *,
    state_db: RAGIngressStateDB,
    source_store: CouchDBSourceStore,
    project: str,
    limit: int,
    max_runtime_seconds: float,
    execute: bool = False,
    expected_plan_digest: str = "",
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Plan or apply a bounded temporal-metadata repair.

    The operation is resumable at each deterministic chunk document.  It never
    deletes data and never changes the source body or content hash.  A source
    revision change makes the session-memory projection pending; the graph
    scheduler's source-hash membership check then treats its prior projection as
    non-current after the session artifact rebuild.
    """

    project = str(project or "").strip()
    _validate_bounds(
        project=project,
        limit=int(limit),
        max_runtime_seconds=float(max_runtime_seconds),
    )
    started = monotonic()
    rows = _payload_rows(state_db, project=project)
    candidates: list[_Candidate] = []
    scanned_count = 0
    metadata_error_count = 0
    integrity_error_count = 0
    delivery_not_succeeded_count = 0
    delivery_hash_mismatch_count = 0
    delivery_scope_mismatch_count = 0
    timed_out = False

    for row in rows:
        if monotonic() - started >= float(max_runtime_seconds):
            timed_out = True
            break
        scanned_count += 1
        if row["delivery_status"] != "succeeded":
            delivery_not_succeeded_count += 1
            continue
        if row["delivery_payload_hash"] != row["payload_hash"]:
            delivery_hash_mismatch_count += 1
            continue
        if (
            row["delivery_target_profile"] != DEFAULT_TRANSCRIPT_TARGET_PROFILE
            or row["delivery_document_kind"] != "conversation_chunk"
        ):
            delivery_scope_mismatch_count += 1
            continue
        payload, gate = resolve_delivery_payload(
            state_db,
            idempotency_key=row["idempotency_key"],
            expected_payload_hash=row["payload_hash"],
        )
        if gate != PAYLOAD_OK or payload is None:
            integrity_error_count += 1
            continue
        try:
            candidates.append(_candidate_from_payload(payload, expected_project=project))
        except (TypeError, ValueError):
            metadata_error_count += 1
    close_rows = getattr(rows, "close", None)
    if callable(close_rows):
        close_rows()

    report: dict[str, Any] = {
        "schema_version": BACKFILL_SCHEMA_VERSION,
        "status": "dry_run" if not execute else "completed",
        "dry_run": not execute,
        "project_scope_hash": sha256_hash(project),
        "limit": int(limit),
        "max_runtime_seconds": float(max_runtime_seconds),
        "scanned_count": scanned_count,
        "scan_exhausted": not timed_out,
        "planned_update_count": 0,
        "total_remaining_update_count": 0,
        "duplicate_count": 0,
        "duplicate_payload_count": 0,
        "superseded_content_count": 0,
        "delivery_not_succeeded_count": delivery_not_succeeded_count,
        "delivery_hash_mismatch_count": delivery_hash_mismatch_count,
        "delivery_scope_mismatch_count": delivery_scope_mismatch_count,
        "source_missing_count": 0,
        "content_conflict_count": 0,
        "wire_conflict_count": 0,
        "metadata_error_count": metadata_error_count,
        "integrity_error_count": integrity_error_count,
        "write_error_count": 0,
        "write_conflict_count": 0,
        "chunk_metadata_write_count": 0,
        "partial_reconciliation_count": 0,
        "updated_count": 0,
        "source_hash_changed_session_count": 0,
        "session_projection_invalidated_count": 0,
        "graph_currentness_invalidated_count": 0,
        "aborted": timed_out,
        "abort_count": 1 if timed_out else 0,
        "timed_out": timed_out,
        "mutation_performed": False,
        "mutation_uncertain": False,
        "mutation_uncertain_count": 0,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
        "plan_digest": _plan_digest([]),
        "expected_plan_digest_match": None,
        "plan_drift_count": 0,
        "gap_count": delivery_not_succeeded_count,
    }

    planned: list[tuple[_Candidate, dict]] = []
    total_remaining_update_count = 0
    grouped: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.document_id, []).append(candidate)
    for document_id, group in grouped.items():
        if monotonic() - started >= float(max_runtime_seconds):
            report["aborted"] = True
            report["timed_out"] = True
            report["scan_exhausted"] = False
            break
        try:
            existing = source_store.get(document_id)
        except Exception:
            report["write_error_count"] += 1
            continue
        if existing is None:
            report["source_missing_count"] += 1
            continue
        if str(existing.get("project") or "") != project:
            report["metadata_error_count"] += 1
            continue
        existing_content_hash = str(existing.get("content_hash") or "")
        if not str(existing.get("_rev") or ""):
            report["integrity_error_count"] += 1
            continue
        if sha256_hash(str(existing.get("body") or "")) != existing_content_hash:
            report["integrity_error_count"] += 1
            continue
        matching = [
            candidate
            for candidate in group
            if candidate.expected_source_content_hash == existing_content_hash
        ]
        if not matching:
            report["content_conflict_count"] += 1
            continue
        report["superseded_content_count"] += len(group) - len(matching)
        by_digest = {candidate.digest: candidate for candidate in matching}
        report["duplicate_payload_count"] += len(matching) - len(by_digest)
        if len(by_digest) != 1:
            report["wire_conflict_count"] += 1
            continue
        candidate = next(iter(by_digest.values()))
        temporal_metadata_matches = (
            str(existing.get("observed_at_start") or "") == candidate.observed_at_start
            and str(existing.get("observed_at_end") or "") == candidate.observed_at_end
        )
        aggregate_current = False
        if temporal_metadata_matches:
            try:
                aggregate_current = _aggregate_is_current(
                    source_store=source_store,
                    session_id_hash=candidate.session_id_hash,
                )
            except Exception:
                report["write_error_count"] += 1
                continue
        if aggregate_current:
            try:
                verified = source_store.get(candidate.document_id) or {}
            except Exception:
                report["write_error_count"] += 1
                continue
            if _chunk_revision_matches(
                verified,
                candidate=candidate,
                expected_rev=str(existing.get("_rev") or ""),
                expected_body=existing.get("body"),
                expected_content_hash=existing.get("content_hash"),
            ):
                report["duplicate_count"] += 1
            else:
                report["write_conflict_count"] += 1
            continue
        total_remaining_update_count += 1
        if len(planned) < int(limit):
            planned.append((candidate, existing))
    report["planned_update_count"] = len(planned)
    report["total_remaining_update_count"] = total_remaining_update_count
    report["plan_digest"] = _plan_digest(planned)

    if execute and expected_plan_digest:
        report["expected_plan_digest_match"] = (
            str(expected_plan_digest or "") == str(report["plan_digest"])
        )
        if report["expected_plan_digest_match"] is not True:
            report["status"] = "blocked_plan_drift"
            report["aborted"] = True
            report["abort_count"] = 1
            report["plan_drift_count"] = 1
            report["error_count"] = 1
            return report

    if execute:
        dirty_sessions: set[str] = set()
        for candidate, existing in planned:
            if monotonic() - started >= float(max_runtime_seconds):
                report["aborted"] = True
                report["timed_out"] = True
                break
            before_body = existing.get("body")
            before_content_hash = existing.get("content_hash")
            operation_mutated = False
            cas_completed = False
            aggregate_started = False
            try:
                revision = source_store.patch_observed_time_if_content_hash(
                    doc_id=candidate.document_id,
                    expected_content_hash=candidate.expected_source_content_hash,
                    expected_rev=str(existing.get("_rev") or ""),
                    observed_at_start=candidate.observed_at_start,
                    observed_at_end=candidate.observed_at_end,
                )
                cas_completed = True
                if str(revision.outcome) != "duplicate":
                    operation_mutated = True
                    report["chunk_metadata_write_count"] += 1
                    report["mutation_performed"] = True
                persisted = source_store.get(candidate.document_id) or {}
                if not _chunk_revision_matches(
                    persisted,
                    candidate=candidate,
                    expected_rev=str(revision.rev or ""),
                    expected_body=before_body,
                    expected_content_hash=before_content_hash,
                ):
                    raise SourceStoreConflict("conditional temporal patch postcheck changed")
                aggregate_started = True
                coverage = update_coverage_with_tool_evidence(
                    session_id_hash=candidate.session_id_hash,
                    store=source_store,
                )
                if coverage is None or not str(coverage.get("source_hash") or ""):
                    raise ValueError("coverage source hash is unavailable")
                post_coverage = source_store.get(candidate.document_id) or {}
                if not _chunk_revision_matches(
                    post_coverage,
                    candidate=candidate,
                    expected_rev=str(revision.rev or ""),
                    expected_body=before_body,
                    expected_content_hash=before_content_hash,
                ):
                    raise SourceStoreConflict("temporal source changed during aggregate refresh")
                mark_projection_pending_if_source_changed(
                    session_id_hash=candidate.session_id_hash,
                    provider=candidate.provider,
                    project=candidate.project,
                    source_hash=str(coverage["source_hash"]),
                    store=source_store,
                    source_changed=True,
                )
            except SourceStoreConflict:
                report["write_conflict_count"] += 1
                if operation_mutated or aggregate_started:
                    report["partial_reconciliation_count"] += 1
                if aggregate_started and not operation_mutated:
                    report["mutation_uncertain"] = True
                    report["mutation_uncertain_count"] += 1
                continue
            except Exception:
                report["write_error_count"] += 1
                if operation_mutated or aggregate_started or not cas_completed:
                    report["partial_reconciliation_count"] += 1
                    if not operation_mutated and (aggregate_started or not cas_completed):
                        report["mutation_uncertain"] = True
                        report["mutation_uncertain_count"] += 1
                continue
            report["updated_count"] += 1
            report["mutation_performed"] = True
            dirty_sessions.add(candidate.session_id_hash)
        dirty_count = len(dirty_sessions)
        report["source_hash_changed_session_count"] = dirty_count
        report["session_projection_invalidated_count"] = dirty_count
        report["graph_currentness_invalidated_count"] = dirty_count

    error_count = (
        int(report["delivery_hash_mismatch_count"])
        + int(report["delivery_scope_mismatch_count"])
        + int(report["metadata_error_count"])
        + int(report["integrity_error_count"])
        + int(report["source_missing_count"])
        + int(report["content_conflict_count"])
        + int(report["wire_conflict_count"])
        + int(report["write_conflict_count"])
        + int(report["write_error_count"])
    )
    report["error_count"] = error_count
    report["abort_count"] = 1 if report["aborted"] else 0
    if report["timed_out"]:
        report["status"] = "aborted_timeout"
    elif error_count:
        report["status"] = "dry_run_with_errors" if not execute else "completed_with_errors"
    elif report["gap_count"]:
        report["status"] = "dry_run_with_gaps" if not execute else "completed_with_gaps"
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge couchdb-temporal-metadata-backfill",
        description="Backfill missing CouchDB observed-time metadata from redacted ingress state.",
    )
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-runtime-seconds", type=float, default=300.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-digest", default="")
    parser.add_argument("--approval", default="")
    return parser


def _error_report(error: str, *, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": BACKFILL_SCHEMA_VERSION,
        "status": "blocked",
        "error": error,
        "dry_run": dry_run,
        "updated_count": 0,
        "error_count": 1,
        "mutation_performed": False,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
    }


def _auth_header(user: str, password: str) -> str:
    if not user:
        return ""
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    execute = bool(args.execute)
    try:
        _validate_bounds(
            project=str(args.project or ""),
            limit=int(args.limit),
            max_runtime_seconds=float(args.max_runtime_seconds),
        )
    except ValueError:
        print(json.dumps(_error_report("invalid_bounds", dry_run=not execute), sort_keys=True))
        return 2

    if execute and re.fullmatch(
        r"sha256:[0-9a-f]{64}", str(args.expected_plan_digest or "")
    ) is None:
        print(
            json.dumps(
                _error_report("expected_plan_digest_invalid", dry_run=False),
                sort_keys=True,
            )
        )
        return 2

    if execute:
        try:
            approval = validate_memory_enqueue_approval(
                args.approval or None,
                operation=BACKFILL_OPERATION,
                command_argv=effective_argv,
            )
            if float(approval.get("timeout_seconds") or 0) < float(args.max_runtime_seconds):
                raise ApprovalError("approval timeout is below execution bound")
        except ApprovalError:
            print(json.dumps(_error_report("approval_rejected", dry_run=False), sort_keys=True))
            return 2

    couchdb_url = os.environ.get("COUCHDB_URL", "")
    if not couchdb_url:
        print(json.dumps(_error_report("env_missing", dry_run=not execute), sort_keys=True))
        return 2
    try:
        state_db = RAGIngressStateDB(args.state_db, read_only=True)
        store = CouchDBHttpSourceStore(
            base_url=couchdb_url,
            db=os.environ.get("COUCHDB_DB", "neurons_transcript_source"),
            auth_header=_auth_header(
                os.environ.get("COUCHDB_USER", ""),
                os.environ.get("COUCHDB_PASSWORD", ""),
            ),
            request_timeout_seconds=min(30.0, float(args.max_runtime_seconds)),
        )
        report = backfill_temporal_metadata(
            state_db=state_db,
            source_store=store,
            project=str(args.project),
            limit=int(args.limit),
            max_runtime_seconds=float(args.max_runtime_seconds),
            execute=execute,
            expected_plan_digest=str(args.expected_plan_digest or ""),
        )
    except Exception:
        print(json.dumps(_error_report("backfill_failed", dry_run=not execute), sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return (
        0
        if int(report.get("error_count") or 0) == 0
        and int(report.get("gap_count") or 0) == 0
        and not report.get("aborted")
        else 1
    )


__all__ = [
    "BACKFILL_OPERATION",
    "BACKFILL_SCHEMA_VERSION",
    "backfill_temporal_metadata",
    "main",
]
