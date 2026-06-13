"""M6 backfill-apply seam: seed/sync a production state DB candidate.

The M4 ``backfill.plan_backfill_from_payloads`` is a pure dry-run planner (no DB
write). This seam adds the approval-gated LIVE counterpart: it applies the planned
rows to a ``RAGIngressStateDB`` candidate so an operator-approved state DB can be
created and then parity-checked with ``rag-ingress-state shadow-readiness``.

Scope and safety:
- It writes ONLY to the new state DB candidate. It never mutates the legacy ledger,
  the source file queue, or RAGFlow.
- It is idempotent: a command whose ``idempotency_key`` already exists is skipped,
  so re-running the apply against a growing source queue is a safe sync, never a
  duplicate insert or a UNIQUE violation.
- ``dry_run`` performs no DB write and returns the planner counts only.
- The report contains counts and booleans only; it never echoes raw idempotency
  keys, payload hashes, job ids, document bodies, or filesystem paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from .backfill import plan_backfill_from_payloads, read_payload_file
from .idempotency import IdempotencyOutcome
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

    Idempotency vs conflict are distinguished by ``payload_hash``: an already-present
    command with the SAME hash is a safe re-sync (``already_present``); the SAME
    idempotency key with a DIFFERENT hash is a genuine conflict (a mutated payload
    re-using a key) and must surface as ``conflict``, not be masked as a clean sync.
    """
    validate_ingress_payload(dict(payload))
    idempotency_key = str(payload["idempotencyKey"])
    payload_hash = str(payload["contentHash"])
    job_id = job_id_for_payload(dict(payload))
    existing_command = state_db.get_row("commands", "idempotency_key", idempotency_key)
    if existing_command is not None:
        if str(existing_command.get("payload_hash") or "") != payload_hash:
            return "conflict"
        # M8.1 fill-gap: candidates seeded before delivery_payloads existed get
        # their payload persisted on the next idempotent re-sync
        if state_db.record_delivery_payload(payload) == "conflict":
            return "conflict"
        return "already_present"
    decision = state_db.record_inbox_shadow(dict(payload), event_id=f"backfill_{job_id}")
    if decision.outcome == IdempotencyOutcome.CONFLICT:
        return "conflict"
    if decision.outcome == IdempotencyOutcome.DUPLICATE:
        return "already_present"
    transaction = state_db.consume_inbox_with_command(
        inbox_event_id=f"backfill_{job_id}",
        command_id=f"cmd_{job_id}",
        command_type="transcript_ingest",
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
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
            )
        ],
    )
    if str(getattr(transaction, "status", "") or "") == "quarantined":
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
