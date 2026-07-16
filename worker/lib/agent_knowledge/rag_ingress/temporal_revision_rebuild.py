"""Additively reconstruct bounded historical session-memory revisions.

The retained ingress payload table contains the already-redacted wire document,
while ``delivery_jobs`` proves whether that exact payload/hash was successfully
delivered.  This module joins both authorities and replays only proven delivery
events into immutable historical artifacts.  It never updates or deletes an
existing artifact and never emits source identifiers or bodies in its report.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..couchdb_source.document_model import (
    build_conversation_chunk_document,
    build_source_hash,
    build_source_revision_token,
    observed_time_bounds,
    sha256_hash,
)
from ..ledger import Ledger
from ..llm_brain_core.artifact_store import SessionMemoryArtifactStore
from ..llm_brain_core.ledger_adapter import LedgerSessionMemoryArtifactStore
from ..llm_brain_core.models import SessionMemoryArtifact
from ..llm_brain_core.runtime import (
    _artifact_search_term_hashes,
    _latest_chunk_hint,
    _source_event_id,
)
from ..session_memory.native_memory_sync_approval import (
    ApprovalError,
    validate_memory_enqueue_approval,
)
from ..session_memory.transcript_model import REDACTION_VERSION, TranscriptChunk
from .delivery_backend import PAYLOAD_OK, resolve_delivery_payload
from .server_runtime import apply_server_redaction
from .state_db import RAGIngressStateDB
from .state_cli import DEFAULT_TRANSCRIPT_TARGET_PROFILE


REBUILD_OPERATION = "couchdb_temporal_revision_rebuild"
REBUILD_SCHEMA_VERSION = "couchdb_temporal_revision_rebuild.v1"
REBUILD_EXTRACTOR_VERSION = "temporal-revision-rebuild.1"
_TARGET_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class _ResolvedRebuildTarget:
    """Immutable writable-ledger snapshot kept separate from public reports."""

    ledger_path: Path = field(repr=False)
    target_fingerprints: dict[str, str]


def _normalized_target_fingerprints(
    target_fingerprints: Mapping[str, object] | None,
) -> dict[str, str]:
    if target_fingerprints is None:
        return {}
    if not isinstance(target_fingerprints, Mapping):
        raise ValueError("target fingerprints must be a mapping")
    normalized: dict[str, str] = {}
    for name, fingerprint in target_fingerprints.items():
        target_name = str(name or "").strip()
        target_value = str(fingerprint or "").strip()
        if not target_name or _TARGET_FINGERPRINT_RE.fullmatch(target_value) is None:
            raise ValueError("target fingerprint is invalid")
        normalized[target_name] = target_value
    return dict(sorted(normalized.items()))


def _target_fingerprint(value: Mapping[str, object]) -> str:
    return sha256_hash(json.dumps(dict(value), sort_keys=True, separators=(",", ":")))


def _target_fingerprint_digest(target_fingerprints: Mapping[str, object] | None) -> str:
    return _target_fingerprint(_normalized_target_fingerprints(target_fingerprints))


def _resolve_rebuild_target(args: argparse.Namespace) -> _ResolvedRebuildTarget:
    """Resolve the writable ledger once so argv aliases cannot drift after approval."""

    ledger_path = Path(str(args.ledger or "")).expanduser().resolve(strict=False)
    fingerprints = _normalized_target_fingerprints(
        {
            "projection_ledger": _target_fingerprint(
                {"kind": "projection_ledger", "path": str(ledger_path)}
            )
        }
    )
    return _ResolvedRebuildTarget(
        ledger_path=ledger_path,
        target_fingerprints=fingerprints,
    )


def _require_approved_target_fingerprints(
    approval: Mapping[str, object],
    *,
    target_fingerprints: Mapping[str, object],
) -> None:
    target = approval.get("target")
    approved = target.get("target_fingerprints") if isinstance(target, Mapping) else None
    if _normalized_target_fingerprints(approved) != _normalized_target_fingerprints(
        target_fingerprints
    ):
        raise ApprovalError("approval target fingerprint mismatch")


@dataclass(frozen=True)
class _PayloadRow:
    idempotency_key: str
    payload_hash: str
    recorded_at: str
    delivery_status: str
    delivery_payload_hash: str
    delivery_target_profile: str
    delivery_document_kind: str


@dataclass(frozen=True)
class _ReplayEvent:
    recorded_at: str
    ordering_key: str
    session_id_hash: str
    provider: str
    project: str
    document: dict[str, Any]

    @property
    def document_id(self) -> str:
        return str(self.document.get("_id") or "")

    @property
    def revision_token(self) -> str:
        return build_source_revision_token(
            self.document,
            material_hash_field="content_hash",
        )

    @property
    def observed_interval(self) -> tuple[str, str]:
        return (
            str(self.document.get("observed_at_start") or ""),
            str(self.document.get("observed_at_end") or ""),
        )


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
) -> Iterator[_PayloadRow]:
    """Stream retained payloads with their exact delivery evidence.

    A left join intentionally keeps unproven payloads visible to the aggregate
    gap report.  Raw keys remain private to the replay planner.
    """

    with state_db.connect() as connection:
        cursor = connection.execute(
            """
            SELECT
                payload.idempotency_key AS idempotency_key,
                payload.payload_hash AS payload_hash,
                payload.recorded_at AS recorded_at,
                COALESCE(job.status, '') AS delivery_status,
                COALESCE(job.payload_hash, '') AS delivery_payload_hash,
                COALESCE(job.target_profile, '') AS delivery_target_profile,
                COALESCE(job.document_kind, '') AS delivery_document_kind
            FROM delivery_payloads AS payload
            LEFT JOIN delivery_jobs AS job
              ON job.idempotency_key = payload.idempotency_key
            WHERE json_valid(payload.payload_json)
              AND COALESCE(
                    NULLIF(
                      json_extract(
                        payload.payload_json,
                        '$.payload.document.metadata.project'
                      ),
                      ''
                    ),
                    json_extract(payload.payload_json, '$.source.project'),
                    ''
                  ) = ?
              AND COALESCE(
                    NULLIF(
                      json_extract(
                        payload.payload_json,
                        '$.payload.document.metadata.type'
                      ),
                      ''
                    ),
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
                yield _PayloadRow(
                    idempotency_key=str(row["idempotency_key"] or ""),
                    payload_hash=str(row["payload_hash"] or ""),
                    recorded_at=str(row["recorded_at"] or ""),
                    delivery_status=str(row["delivery_status"] or ""),
                    delivery_payload_hash=str(row["delivery_payload_hash"] or ""),
                    delivery_target_profile=str(row["delivery_target_profile"] or ""),
                    delivery_document_kind=str(row["delivery_document_kind"] or ""),
                )


def _aware_iso8601(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is missing")
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return text


def _safe_int(value: object, *, default: int) -> int:
    if value in (None, ""):
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError("positional metadata must be non-negative")
    return parsed


def _event_from_payload(
    payload: dict[str, Any],
    *,
    expected_project: str,
    recorded_at: str,
    ordering_key: str,
) -> _ReplayEvent:
    """Build the same canonical chunk shape as live CouchDB delivery."""

    redacted_payload = apply_server_redaction(payload)
    package = redacted_payload.get("payload") or {}
    document = package.get("document") or {}
    metadata = document.get("metadata") or {}
    source = redacted_payload.get("source") or {}
    if not isinstance(document, Mapping) or not isinstance(metadata, Mapping):
        raise ValueError("wire document metadata shape is invalid")
    if not isinstance(source, Mapping):
        raise ValueError("wire source metadata shape is invalid")

    project = str(metadata.get("project") or source.get("project") or "")
    if project != expected_project:
        raise ValueError("wire project scope mismatch")
    session_id_hash = str(metadata.get("session_id_hash") or "")
    chunk_id = str(metadata.get("chunk_id") or "")
    if not session_id_hash or not chunk_id:
        raise ValueError("wire source identity metadata is missing")

    observed_at_start = _aware_iso8601(
        metadata.get("observed_at_start"),
        field="observed_at_start",
    )
    observed_at_end = _aware_iso8601(
        metadata.get("observed_at_end"),
        field="observed_at_end",
    )
    start = datetime.datetime.fromisoformat(observed_at_start.replace("Z", "+00:00"))
    end = datetime.datetime.fromisoformat(observed_at_end.replace("Z", "+00:00"))
    if end < start:
        raise ValueError("wire temporal range is invalid")
    _aware_iso8601(recorded_at, field="recorded_at")

    body = str(document.get("body") or "")
    turn_start_index = _safe_int(metadata.get("turn_start_index"), default=0)
    turn_end_index = _safe_int(metadata.get("turn_end_index"), default=0)
    part_index = _safe_int(metadata.get("part_index"), default=1)
    part_count = _safe_int(metadata.get("part_count"), default=1)
    char_start = _safe_int(metadata.get("char_start"), default=0)
    char_end = _safe_int(metadata.get("char_end"), default=len(body))
    if turn_end_index < turn_start_index or part_count < 1 or part_index < 1:
        raise ValueError("wire positional range is invalid")
    if part_index > part_count or char_end < char_start:
        raise ValueError("wire positional range is invalid")

    provider = str(
        metadata.get("provider")
        or source.get("provider")
        or source.get("namespace")
        or "ingress"
    )
    chunk = TranscriptChunk(
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        turn_start_index=turn_start_index,
        turn_end_index=turn_end_index,
        redacted_text=body,
        content_hash=sha256_hash(body),
        redaction_version=str(package.get("redactionVersion") or REDACTION_VERSION),
        source_status="source_locator_private_spool_only",
        part_index=part_index,
        part_count=part_count,
        char_start=char_start,
        char_end=char_end,
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
    )
    chunk_document = build_conversation_chunk_document(
        chunk=chunk,
        source_locator_hash="",
    )
    return _ReplayEvent(
        recorded_at=recorded_at,
        ordering_key=ordering_key,
        session_id_hash=session_id_hash,
        provider=str(chunk_document.get("provider") or provider),
        project=str(chunk_document.get("project") or project),
        document=chunk_document,
    )


def _snapshot_artifact(
    *,
    event: _ReplayEvent,
    snapshot: Mapping[str, Mapping[str, Any]],
    materialization_revision: int,
) -> SessionMemoryArtifact:
    documents = [dict(snapshot[key]) for key in sorted(snapshot)]
    observed_at_start, observed_at_end = observed_time_bounds(
        sessions=[],
        chunks=documents,
    )
    source_revision = build_source_hash(
        [str(document.get("content_hash") or "") for document in documents],
        [],
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        conversation_revision_tokens=[
            build_source_revision_token(document, material_hash_field="content_hash")
            for document in documents
        ],
    )
    interval = event.observed_interval
    summary = (
        f"Session artifact for {event.provider}/{event.project}. "
        f"conversation_chunks={len(documents)}. "
        "tool_evidence_bundles=0. "
        f"{_latest_chunk_hint(documents)} "
        "retained_delivery_replay=bounded."
    )
    return SessionMemoryArtifact.from_summary(
        session_id_hash=event.session_id_hash,
        project=event.project,
        provider=event.provider,
        summary=summary,
        source_event_ids=[_source_event_id(document) for document in documents],
        chunk_refs=[str(document.get("_id") or "") for document in documents],
        ontology_version="1.0.0",
        extractor_version=REBUILD_EXTRACTOR_VERSION,
        created_at=interval[0],
        source_revision=source_revision,
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        revision_observed_at_start=interval[0],
        revision_observed_at_end=interval[1],
        revision_observed_intervals=[interval],
        revision_temporal_evidence="bounded",
        # Relevance belongs to the revision delta, not the cumulative prefix.
        # Otherwise a later snapshot can inherit an older subject and falsely
        # satisfy a date-scoped semantic query.
        search_term_hashes=_artifact_search_term_hashes(
            chunks=[event.document],
            evidence=[],
        ),
        materialized_at=event.recorded_at,
        materialization_revision=materialization_revision,
    )


def _replay_artifacts(
    events: list[_ReplayEvent],
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> tuple[list[SessionMemoryArtifact], int, bool]:
    grouped: dict[str, list[_ReplayEvent]] = {}
    for event in events:
        if monotonic() >= deadline:
            return [], 0, True
        grouped.setdefault(event.session_id_hash, []).append(event)
    if monotonic() >= deadline:
        return [], 0, True

    artifacts: list[SessionMemoryArtifact] = []
    exact_duplicate_count = 0
    session_id_hashes = sorted(grouped)
    if monotonic() >= deadline:
        return [], exact_duplicate_count, True
    for session_id_hash in session_id_hashes:
        snapshot: dict[str, dict[str, Any]] = {}
        snapshot_tokens: dict[str, str] = {}
        materialization_revision = 0
        session_events = sorted(
            grouped[session_id_hash],
            key=lambda item: (item.recorded_at, item.ordering_key),
        )
        if monotonic() >= deadline:
            return [], exact_duplicate_count, True
        for event in session_events:
            if monotonic() >= deadline:
                return [], exact_duplicate_count, True
            if snapshot_tokens.get(event.document_id) == event.revision_token:
                exact_duplicate_count += 1
                if monotonic() >= deadline:
                    return [], exact_duplicate_count, True
                continue
            snapshot[event.document_id] = event.document
            snapshot_tokens[event.document_id] = event.revision_token
            materialization_revision += 1
            artifacts.append(
                _snapshot_artifact(
                    event=event,
                    snapshot=snapshot,
                    materialization_revision=materialization_revision,
                )
            )
            if monotonic() >= deadline:
                return [], exact_duplicate_count, True
    return artifacts, exact_duplicate_count, False


def _plan_digest(
    artifacts: list[SessionMemoryArtifact],
    *,
    target_fingerprints: Mapping[str, object] | None = None,
) -> str:
    items = [
        json.dumps(
            {
                "artifact_identity_hash": sha256_hash(artifact.artifact_id),
                "content_hash": artifact.content_hash,
                "source_revision": artifact.source_revision,
                "revision_interval_hash": sha256_hash(
                    json.dumps(
                        list(artifact.revision_observed_intervals),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                "materialization_revision": artifact.materialization_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for artifact in artifacts
    ]
    return sha256_hash(
        json.dumps(
            {
                "artifacts": sorted(items),
                "target_fingerprint": _target_fingerprint_digest(target_fingerprints),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _base_report(
    *,
    project: str,
    limit: int,
    max_runtime_seconds: float,
    execute: bool,
    target_fingerprints: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    normalized_target_fingerprints = _normalized_target_fingerprints(target_fingerprints)
    return {
        "schema_version": REBUILD_SCHEMA_VERSION,
        "status": "completed" if execute else "dry_run",
        "dry_run": not execute,
        "project_scope_hash": sha256_hash(project),
        "limit": int(limit),
        "max_runtime_seconds": float(max_runtime_seconds),
        "scanned_payload_count": 0,
        "succeeded_delivery_count": 0,
        "delivery_not_succeeded_count": 0,
        "delivery_hash_mismatch_count": 0,
        "delivery_scope_mismatch_count": 0,
        "integrity_error_count": 0,
        "metadata_error_count": 0,
        "exact_duplicate_payload_count": 0,
        "existing_artifact_count": 0,
        "planned_artifact_count": 0,
        "total_remaining_artifact_count": 0,
        "inserted_artifact_count": 0,
        "duplicate_artifact_count": 0,
        "write_error_count": 0,
        "postcheck_error_count": 0,
        "gap_count": 0,
        "error_count": 0,
        "scan_exhausted": True,
        "timed_out": False,
        "aborted": False,
        "abort_count": 0,
        "mutation_performed": False,
        "mutation_uncertain": False,
        "current_materialization_rebuild_required": False,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
        "target_fingerprints": normalized_target_fingerprints,
        "target_fingerprint": _target_fingerprint_digest(normalized_target_fingerprints),
        "plan_digest": _plan_digest(
            [],
            target_fingerprints=normalized_target_fingerprints,
        ),
        "expected_plan_digest_match": None,
        "plan_drift_count": 0,
    }


def _finish_report(report: dict[str, Any], *, execute: bool) -> dict[str, Any]:
    report["gap_count"] = (
        int(report["delivery_not_succeeded_count"])
        + int(report["delivery_hash_mismatch_count"])
        + int(report["delivery_scope_mismatch_count"])
        + int(report["integrity_error_count"])
        + int(report["metadata_error_count"])
    )
    report["error_count"] = (
        int(report["delivery_hash_mismatch_count"])
        + int(report["delivery_scope_mismatch_count"])
        + int(report["integrity_error_count"])
        + int(report["metadata_error_count"])
        + int(report["write_error_count"])
        + int(report["postcheck_error_count"])
    )
    report["abort_count"] = 1 if report["aborted"] else 0
    if report["timed_out"]:
        report["status"] = "aborted_timeout"
    elif report["error_count"]:
        report["status"] = "completed_with_errors" if execute else "dry_run_with_errors"
    elif report["gap_count"]:
        report["status"] = "completed_with_gaps" if execute else "dry_run_with_gaps"
    return report


def rebuild_temporal_revisions(
    *,
    state_db: RAGIngressStateDB,
    artifact_store: SessionMemoryArtifactStore,
    project: str,
    limit: int,
    max_runtime_seconds: float,
    execute: bool = False,
    expected_plan_digest: str = "",
    target_fingerprints: Mapping[str, object] | None = None,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Plan or add bounded historical artifacts from proven retained deliveries."""

    project = str(project or "").strip()
    _validate_bounds(
        project=project,
        limit=int(limit),
        max_runtime_seconds=float(max_runtime_seconds),
    )
    report = _base_report(
        project=project,
        limit=int(limit),
        max_runtime_seconds=float(max_runtime_seconds),
        execute=execute,
        target_fingerprints=target_fingerprints,
    )
    started = monotonic()
    local_deadline = started + float(max_runtime_seconds)
    effective_deadline = (
        min(local_deadline, float(deadline))
        if deadline is not None
        else local_deadline
    )
    if not math.isfinite(effective_deadline):
        raise ValueError("deadline must be finite")

    def _deadline_reached() -> bool:
        return monotonic() >= effective_deadline

    def _abort_timeout() -> dict[str, Any]:
        report["scan_exhausted"] = False
        report["timed_out"] = True
        report["aborted"] = True
        report["planned_artifact_count"] = 0
        report["total_remaining_artifact_count"] = 0
        return _finish_report(report, execute=execute)

    if started >= effective_deadline:
        return _abort_timeout()

    events: list[_ReplayEvent] = []
    rows = _payload_rows(state_db, project=project)
    for row in rows:
        if _deadline_reached():
            report["scan_exhausted"] = False
            report["timed_out"] = True
            report["aborted"] = True
            break
        report["scanned_payload_count"] += 1
        if row.delivery_status != "succeeded":
            report["delivery_not_succeeded_count"] += 1
            continue
        if row.delivery_payload_hash != row.payload_hash:
            report["delivery_hash_mismatch_count"] += 1
            continue
        if (
            row.delivery_target_profile != DEFAULT_TRANSCRIPT_TARGET_PROFILE
            or row.delivery_document_kind != "conversation_chunk"
        ):
            report["delivery_scope_mismatch_count"] += 1
            continue
        payload, gate = resolve_delivery_payload(
            state_db,
            idempotency_key=row.idempotency_key,
            expected_payload_hash=row.payload_hash,
        )
        if gate != PAYLOAD_OK or payload is None:
            report["integrity_error_count"] += 1
            continue
        report["succeeded_delivery_count"] += 1
        try:
            events.append(
                _event_from_payload(
                    payload,
                    expected_project=project,
                    recorded_at=row.recorded_at,
                    ordering_key=row.idempotency_key,
                )
            )
        except (TypeError, ValueError):
            report["metadata_error_count"] += 1
    close_rows = getattr(rows, "close", None)
    if callable(close_rows):
        close_rows()

    if _deadline_reached():
        report["scan_exhausted"] = False
        report["timed_out"] = True
        report["aborted"] = True

    # Never derive or execute a partial history when the authoritative scan did
    # not complete inside its bound.
    if report["timed_out"]:
        report["planned_artifact_count"] = 0
        report["total_remaining_artifact_count"] = 0
        return _finish_report(report, execute=execute)

    artifacts, exact_duplicate_count, replay_timed_out = _replay_artifacts(
        events,
        deadline=effective_deadline,
        monotonic=monotonic,
    )
    if replay_timed_out:
        return _abort_timeout()
    report["exact_duplicate_payload_count"] = exact_duplicate_count
    remaining: list[SessionMemoryArtifact] = []
    for artifact in artifacts:
        if _deadline_reached():
            return _abort_timeout()
        try:
            existing = artifact_store.get(artifact.artifact_id)
        except Exception:
            report["write_error_count"] += 1
            continue
        if _deadline_reached():
            return _abort_timeout()
        if existing is None:
            remaining.append(artifact)
        elif existing.content_hash == artifact.content_hash:
            report["existing_artifact_count"] += 1
        else:
            # The store would reject this identity collision too. Surface it in
            # the plan and do not attempt a write.
            report["write_error_count"] += 1

    planned = remaining[: int(limit)]
    report["total_remaining_artifact_count"] = len(remaining)
    report["planned_artifact_count"] = len(planned)
    report["plan_digest"] = _plan_digest(
        planned,
        target_fingerprints=report["target_fingerprints"],
    )
    if _deadline_reached():
        return _abort_timeout()

    if execute:
        report["expected_plan_digest_match"] = (
            bool(expected_plan_digest)
            and str(expected_plan_digest) == str(report["plan_digest"])
        )
        if report["expected_plan_digest_match"] is not True:
            report["status"] = "blocked_plan_drift"
            report["aborted"] = True
            report["abort_count"] = 1
            report["plan_drift_count"] = 1
            report["error_count"] = 1
            return report

        for artifact in planned:
            if _deadline_reached():
                report["timed_out"] = True
                report["aborted"] = True
                break
            try:
                outcome = artifact_store.upsert(artifact)
            except Exception:
                # The store may have committed before losing its acknowledgement.
                # Without an outcome or postcheck the mutation state is unknown.
                report["write_error_count"] += 1
                report["mutation_uncertain"] = True
                if _deadline_reached():
                    report["timed_out"] = True
                    report["aborted"] = True
                    break
                continue

            if outcome == "inserted":
                report["inserted_artifact_count"] += 1
                report["mutation_performed"] = True
                report["current_materialization_rebuild_required"] = True
            elif outcome == "duplicate":
                report["duplicate_artifact_count"] += 1
            else:
                report["write_error_count"] += 1
                report["mutation_uncertain"] = True
                if _deadline_reached():
                    report["timed_out"] = True
                    report["aborted"] = True
                    break
                continue

            if _deadline_reached():
                report["timed_out"] = True
                report["aborted"] = True
                report["mutation_uncertain"] = True
                break

            try:
                persisted = artifact_store.get(artifact.artifact_id)
            except Exception:
                report["postcheck_error_count"] += 1
                report["mutation_uncertain"] = True
                if _deadline_reached():
                    report["timed_out"] = True
                    report["aborted"] = True
                    break
                continue

            if _deadline_reached():
                report["timed_out"] = True
                report["aborted"] = True
                report["mutation_uncertain"] = True
                break
            if persisted is None or persisted.content_hash != artifact.content_hash:
                report["postcheck_error_count"] += 1
                report["mutation_uncertain"] = True

    return _finish_report(report, execute=execute)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge couchdb-temporal-revision-rebuild",
        description=(
            "Additively rebuild bounded historical session-memory revisions "
            "from proven retained deliveries."
        ),
    )
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--ledger", required=True)
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
        "schema_version": REBUILD_SCHEMA_VERSION,
        "status": "blocked",
        "error": error,
        "dry_run": dry_run,
        "inserted_artifact_count": 0,
        "error_count": 1,
        "mutation_performed": False,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
    }


def _read_only_plan(
    *,
    state_db: RAGIngressStateDB,
    ledger_path: Path,
    target_fingerprints: Mapping[str, object],
    project: str,
    limit: int,
    max_runtime_seconds: float,
    deadline: float,
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    ledger = Ledger.open_read_only(str(ledger_path))
    return rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=LedgerSessionMemoryArtifactStore(ledger),
        project=project,
        limit=limit,
        max_runtime_seconds=max_runtime_seconds,
        target_fingerprints=target_fingerprints,
        deadline=deadline,
        monotonic=monotonic,
    )


def main(
    argv: list[str] | None = None,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
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
    command_deadline = monotonic() + float(args.max_runtime_seconds)

    try:
        target = _resolve_rebuild_target(args)
    except Exception:
        print(json.dumps(_error_report("invalid_target", dry_run=not execute), sort_keys=True))
        return 2

    if execute and re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        str(args.expected_plan_digest or ""),
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
                operation=REBUILD_OPERATION,
                command_argv=effective_argv,
            )
            if float(approval.get("timeout_seconds") or 0) < float(
                args.max_runtime_seconds
            ):
                raise ApprovalError("approval timeout is below execution bound")
            _require_approved_target_fingerprints(
                approval,
                target_fingerprints=target.target_fingerprints,
            )
        except ApprovalError:
            print(
                json.dumps(
                    _error_report("approval_rejected", dry_run=False),
                    sort_keys=True,
                )
            )
            return 2

    try:
        state_db = RAGIngressStateDB(args.state_db, read_only=True)
        plan = _read_only_plan(
            state_db=state_db,
            ledger_path=target.ledger_path,
            target_fingerprints=target.target_fingerprints,
            project=str(args.project),
            limit=int(args.limit),
            max_runtime_seconds=float(args.max_runtime_seconds),
            deadline=command_deadline,
            monotonic=monotonic,
        )
    except Exception:
        print(json.dumps(_error_report("rebuild_failed", dry_run=not execute), sort_keys=True))
        return 1

    if not execute:
        print(json.dumps(plan, sort_keys=True))
        return (
            0
            if int(plan.get("error_count") or 0) == 0
            and int(plan.get("gap_count") or 0) == 0
            and not plan.get("aborted")
            else 1
        )

    if str(plan.get("plan_digest") or "") != str(args.expected_plan_digest or ""):
        plan["status"] = "blocked_plan_drift"
        plan["dry_run"] = False
        plan["aborted"] = True
        plan["abort_count"] = 1
        plan["expected_plan_digest_match"] = False
        plan["plan_drift_count"] = 1
        plan["error_count"] = max(1, int(plan.get("error_count") or 0))
        print(json.dumps(plan, sort_keys=True))
        return 1

    try:
        # Writable ledger access is delayed until exact argv approval and a fresh
        # read-only plan digest have both passed.
        ledger = Ledger(str(target.ledger_path), initialize_schema=False)
        report = rebuild_temporal_revisions(
            state_db=state_db,
            artifact_store=LedgerSessionMemoryArtifactStore(ledger),
            project=str(args.project),
            limit=int(args.limit),
            max_runtime_seconds=float(args.max_runtime_seconds),
            execute=True,
            expected_plan_digest=str(args.expected_plan_digest),
            target_fingerprints=target.target_fingerprints,
            deadline=command_deadline,
            monotonic=monotonic,
        )
    except Exception:
        print(json.dumps(_error_report("rebuild_failed", dry_run=False), sort_keys=True))
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
    "REBUILD_OPERATION",
    "REBUILD_SCHEMA_VERSION",
    "main",
    "rebuild_temporal_revisions",
]
