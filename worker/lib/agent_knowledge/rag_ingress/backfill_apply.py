"""M6 backfill-apply seam: seed/sync a production state DB candidate.

The M4 ``backfill.plan_backfill_from_payloads`` is a pure dry-run planner (no DB
write). This seam adds the approval-gated LIVE counterpart: it applies the planned
rows to a ``RAGIngressStateDB`` candidate so an operator-approved state DB can be
created and then parity-checked with ``rag-ingress-state shadow-readiness``.

Scope and safety:
- It writes ONLY to the new state DB candidate. It never mutates the legacy ledger,
  the source file queue, or RetiredIndexBridge.
- It is idempotent for identical canonical envelopes: an existing command with
  the same ``idempotencyKey`` and envelope fingerprint is skipped, while a
  mutated envelope re-using the key is reported as a conflict.
- ``dry_run`` performs no DB write and returns the planner counts only.
- The report contains counts and booleans only; it never echoes raw idempotency
  keys, payload hashes, job ids, document bodies, or filesystem paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from .backfill import plan_backfill_from_payloads, read_payload_file
from .idempotency import IdempotencyOutcome, envelope_fingerprint
from .server_runtime import job_id_for_payload, validate_ingress_payload
from .state_db import (
    CommandResultSpec,
    DeliveryJobSpec,
    DomainRecordSpec,
    RAGIngressStateDB,
)


def read_queue_payloads(queue_root: Path | str) -> list[dict]:
    """Read every ``*.json`` ingress payload under the source queue root (read-only)."""
    root = Path(queue_root)
    payloads: list[dict] = []
    if not root.exists():
        return payloads
    for path in sorted(root.rglob("*.json")):
        if not path.is_file():
            continue
        payloads.append(read_payload_file(path))
    return payloads


def _apply_one(state_db: RAGIngressStateDB, payload: Mapping[str, object]) -> str:
    """Apply a single payload to the candidate; return an outcome label.

    The authoritative command row (checked atomically by ``record_inbox_shadow``)
    gates command and delivery-job creation. Identical canonical envelopes are a
    safe re-sync (``already_present``); any envelope mutation re-using the key --
    including metadata-only changes -- is a ``conflict``, never a clean sync.
    """
    validate_ingress_payload(dict(payload))
    idempotency_key = str(payload["idempotencyKey"])
    payload_hash = str(payload["contentHash"])
    fingerprint = envelope_fingerprint(payload)
    job_id = job_id_for_payload(dict(payload))
    event_id = f"backfill_{job_id}"

    decision = state_db.record_inbox_shadow(dict(payload), event_id=event_id)
    if decision.outcome == IdempotencyOutcome.CONFLICT:
        return "conflict"
    if decision.outcome == IdempotencyOutcome.QUARANTINED:
        return "conflict"
    if decision.outcome in (
        IdempotencyOutcome.DUPLICATE,
        IdempotencyOutcome.REPLAYABLE,
    ):
        # M8.1 fill-gap: candidates seeded before delivery_payloads existed get
        # their payload persisted on the next idempotent re-sync.
        if state_db.record_delivery_payload(payload) == "conflict":
            return "conflict"
        return "already_present"

    try:
        transaction = state_db.consume_inbox_with_command(
            inbox_event_id=event_id,
            command_id=f"cmd_{job_id}",
            command_type="transcript_ingest",
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            envelope_fingerprint=fingerprint,
            result=CommandResultSpec(decision="completed"),
            domain_records=[
                DomainRecordSpec(
                    domain_record_id=f"domain_{job_id}",
                    domain_kind="delivery_projection",
                    lifecycle_status="prepared",
                    payload_hash=payload_hash,
                )
            ],
            delivery_jobs=[
                DeliveryJobSpec(
                    job_id=job_id,
                    target_profile=str(payload["targetProfile"]),
                    document_kind=str(payload["kind"]),
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    envelope_fingerprint=fingerprint,
                )
            ],
        )
    except sqlite3.IntegrityError:
        # A concurrent writer won the commands.idempotency_key race.
        return "conflict"
    if str(getattr(transaction, "status", "") or "") in {"quarantined", "conflict"}:
        return "conflict"
    if state_db.record_delivery_payload(payload) == "conflict":
        return "conflict"
    return "applied"


def apply_backfill_to_state_db(
    *,
    state_db: RAGIngressStateDB,
    payloads: Iterable[Mapping[str, object]],
    dry_run: bool = True,
) -> dict:
    """Plan (dry-run) or apply (live) a backfill onto the state DB candidate."""
    payload_list = [dict(payload) for payload in payloads]

    if dry_run:
        plan = plan_backfill_from_payloads(payload_list)
        return {
            "schema_version": "agent_knowledge_rag_ingress_backfill_apply.v1",
            "dry_run": True,
            "payload_count": len(payload_list),
            "planned_count": len(plan["planned_rows"]),
            "duplicate_count": len(plan["replay"]),
            "conflict_count": len(plan["quarantine"]),
            "blocker_count": len(plan["blockers"]),
            "applied_count": 0,
            "already_present_count": 0,
            "cutover_status": plan["cutover_status"],
            "mutation_performed": False,
            "network_used": False,
            "raw_ids_printed": False,
            "raw_paths_printed": False,
        }

    applied = 0
    already_present = 0
    conflict = 0
    for payload in payload_list:
        outcome = _apply_one(state_db, payload)
        if outcome == "applied":
            applied += 1
        elif outcome == "conflict":
            conflict += 1
        else:
            already_present += 1

    return {
        "schema_version": "agent_knowledge_rag_ingress_backfill_apply.v1",
        "dry_run": False,
        "payload_count": len(payload_list),
        "planned_count": applied,
        "duplicate_count": already_present,
        "conflict_count": conflict,
        "blocker_count": conflict,
        "applied_count": applied,
        "already_present_count": already_present,
        "cutover_status": "cutover_blocked" if conflict else "migration_ready_pending_approval",
        "mutation_performed": bool(applied),
        "network_used": False,
        "raw_ids_printed": False,
        "raw_paths_printed": False,
    }
