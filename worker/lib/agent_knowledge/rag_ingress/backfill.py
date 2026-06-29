"""M4 dry-run backfill and migration readiness helpers.

The functions here are deliberately pure over source queue payloads. They do
not mutate legacy queues, the legacy ledger, the new state DB, or RetiredIndexBridge.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .idempotency import IdempotencyOutcome, classify_idempotency
from .server_runtime import job_id_for_payload, validate_ingress_payload
from .state_db import RAGIngressStateDB


DEFAULT_PROJECTION_FRESHNESS_SECONDS = 3600


@dataclass(frozen=True)
class SourceQueueSnapshot:
    path_hashes: dict[str, str]

    @property
    def file_count(self) -> int:
        return len(self.path_hashes)


def snapshot_queue_files(queue_root: Path | str) -> SourceQueueSnapshot:
    root = Path(queue_root)
    path_hashes: dict[str, str] = {}
    if not root.exists():
        return SourceQueueSnapshot(path_hashes)
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root).as_posix()
        path_hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return SourceQueueSnapshot(path_hashes)


def state_db_counts(state_db: RAGIngressStateDB) -> dict[str, int]:
    return {
        name: int(state_db.scalar(f"SELECT COUNT(*) FROM {name}") or 0)
        for name in ("inbox_events", "commands", "domain_records", "command_results", "delivery_jobs")
    }


def plan_backfill_from_payloads(payloads: Iterable[Mapping[str, object]]) -> dict:
    seen: dict[str, dict] = {}
    planned_rows: list[dict] = []
    quarantine: list[dict] = []
    replay: list[dict] = []
    blockers: list[dict] = []

    for index, payload_mapping in enumerate(payloads):
        payload = dict(payload_mapping)
        validate_ingress_payload(payload)
        idempotency_key = str(payload["idempotencyKey"])
        payload_hash = str(payload["contentHash"])
        existing = seen.get(idempotency_key)
        decision = classify_idempotency(existing, idempotency_key=idempotency_key, payload_hash=payload_hash)
        event_id = f"backfill_{job_id_for_payload(payload)}_{index}"
        if decision.outcome == IdempotencyOutcome.ACCEPTED:
            job_id = job_id_for_payload(payload)
            row_bundle = {
                "inbox_event": {
                    "event_id": event_id,
                    "idempotency_key": idempotency_key,
                    "payload_hash": payload_hash,
                    "accept_outcome": "accepted",
                },
                "command": {
                    "command_id": f"cmd_{job_id}",
                    "command_type": "transcript_ingest",
                    "idempotency_key": idempotency_key,
                    "payload_hash": payload_hash,
                },
                "domain_record": {
                    "domain_record_id": f"domain_{job_id}",
                    "domain_kind": "delivery_projection",
                    "lifecycle_status": "prepared",
                    "payload_hash": payload_hash,
                },
                "delivery_job": {
                    "job_id": job_id,
                    "idempotency_key": idempotency_key,
                    "payload_hash": payload_hash,
                    "target_profile": str(payload["targetProfile"]),
                    "document_kind": str(payload["kind"]),
                },
            }
            planned_rows.append(row_bundle)
            seen[idempotency_key] = {
                "idempotency_key": idempotency_key,
                "payload_hash": payload_hash,
                "accept_outcome": "accepted",
                "job_id": job_id,
            }
            continue
        if decision.outcome == IdempotencyOutcome.DUPLICATE:
            replay.append(
                {
                    "event_id": event_id,
                    "outcome": "duplicate",
                    "idempotency_key": idempotency_key,
                    "converges_to_job_id": existing.get("job_id") if existing else "",
                }
            )
            continue
        quarantine.append(
            {
                "event_id": event_id,
                "outcome": "conflict",
                "idempotency_key": idempotency_key,
                "reason": decision.reason,
            }
        )
        blockers.append(
            {
                "code": "idempotency_conflict",
                "severity": "blocking",
                "idempotency_key": idempotency_key,
            }
        )

    return build_readiness_report(
        planned_rows=planned_rows,
        quarantine=quarantine,
        replay=replay,
        blockers=blockers,
        required_evidence=["fixture_payloads", "dry_run_no_mutation"],
    )


def evaluate_delivery_readiness(
    delivery_rows: Iterable[Mapping[str, object]],
    *,
    now: datetime,
    projection_freshness_seconds: int = DEFAULT_PROJECTION_FRESHNESS_SECONDS,
) -> dict:
    blockers: list[dict] = []
    replay: list[dict] = []
    quarantine: list[dict] = []
    for row in delivery_rows:
        job_id = str(row.get("job_id") or "")
        status = str(row.get("status") or "")
        if status == "replayable":
            replay.append({"job_id": job_id, "outcome": "replayable"})
            blockers.append({"code": "replayable_unresolved", "severity": "blocking", "job_id": job_id})
        if status == "failed_retryable":
            blockers.append({"code": "async_fail_unresolved", "severity": "blocking", "job_id": job_id})
        if status == "quarantined":
            quarantine.append({"job_id": job_id, "outcome": "quarantined"})
            blockers.append({"code": "quarantined_unresolved", "severity": "blocking", "job_id": job_id})
        last_reconciled_at = str(row.get("last_reconciled_at") or "")
        if _projection_is_stale(last_reconciled_at, now, projection_freshness_seconds):
            blockers.append({"code": "stale_projection", "severity": "blocking", "job_id": job_id})
    return build_readiness_report(
        planned_rows=[],
        quarantine=quarantine,
        replay=replay,
        blockers=blockers,
        required_evidence=["fresh_projection", "operator_replay_packet"],
    )


def build_readiness_report(
    *,
    planned_rows: list[dict],
    quarantine: list[dict],
    replay: list[dict],
    blockers: list[dict],
    required_evidence: list[str],
    rollback_owner: str = "neurons",
) -> dict:
    return {
        "cutover_status": "cutover_blocked" if blockers else "migration_ready_pending_approval",
        "blockers": blockers,
        "planned_rows": planned_rows,
        "quarantine": quarantine,
        "replay": replay,
        "rollback_owner": rollback_owner,
        "required_evidence": required_evidence,
    }


def read_payload_file(path: Path | str) -> dict:
    source = Path(path)
    # fail-closed, consistent with the state DB / ingress journal symlink policy;
    # the message stays path-free so callers can echo it redacted
    if source.is_symlink():
        raise ValueError("payload file must not be a symlink")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("payload file must contain a JSON object")
    return payload


def _projection_is_stale(value: str, now: datetime, freshness_seconds: int) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() > freshness_seconds
