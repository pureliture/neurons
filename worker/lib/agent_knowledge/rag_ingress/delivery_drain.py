"""M8.1 worker drain seam: pull ``pending`` delivery_jobs through DeliveryExecutor.

dry-run-first by contract:
- ``dry_run=True`` (default) performs NO claim, NO state-DB mutation, NO backend
  call. It selects pending jobs and runs only the read-only payload availability
  gate (Slice A), so an operator can see whether a drain would even have payloads
  to deliver.
- live (``dry_run=False``) requires an injected backend and is reached only from
  the operator-gated CLI path (explicit approval record). Each job goes through
  ``DeliveryExecutor.execute_once`` (claim -> submit outside the transaction ->
  evidence). Bounded by ``limit`` and ``max_runtime_seconds``.

No LaunchAgent/cron/server path imports this module; default runtime never drains.
The report contains counts/booleans only -- never job ids, payload bodies, or paths.
"""

from __future__ import annotations

import time

from .delivery_backend import PAYLOAD_OK, resolve_delivery_payload
from .delivery_executor import DeliveryBackend, DeliveryExecutor
from .state_db import RAGIngressStateDB

_BLOCKED_OUTCOMES = {"claim_rejected", "stale_owner_rejected"}


def drain_pending_deliveries(
    *,
    state_db: RAGIngressStateDB,
    backend: DeliveryBackend | None = None,
    lease_owner: str = "m8_drain",
    limit: int = 10,
    dry_run: bool = True,
    max_attempts: int = 3,
    max_runtime_seconds: float = 300.0,
) -> dict:
    selected = state_db.list_delivery_jobs(status="pending", limit=limit)

    payload_available = 0
    payload_missing = 0
    payload_hash_mismatch = 0
    for row in selected:
        _payload, gate = resolve_delivery_payload(
            state_db,
            idempotency_key=str(row.get("idempotency_key") or ""),
            expected_payload_hash=str(row.get("payload_hash") or ""),
        )
        if gate == PAYLOAD_OK:
            payload_available += 1
        elif gate == "payload_missing":
            payload_missing += 1
        else:
            payload_hash_mismatch += 1

    blockers: list[str] = []
    if payload_missing:
        blockers.append("delivery_payload_missing")
    if payload_hash_mismatch:
        blockers.append("delivery_payload_hash_mismatch")

    claimed = 0
    executed = 0
    succeeded = 0
    retryable = 0
    quarantined = 0
    blocked = 0
    runtime_exceeded = False
    if not dry_run:
        if backend is None:
            raise ValueError("live drain requires a delivery backend")
        executor = DeliveryExecutor(state_db=state_db, backend=backend, lease_owner=lease_owner)
        started = time.monotonic()
        for row in selected:
            if time.monotonic() - started > max_runtime_seconds:
                runtime_exceeded = True
                blockers.append("max_runtime_exceeded")
                break
            outcome = executor.execute_once(str(row["job_id"]), max_attempts=max_attempts)
            executed += 1
            if outcome in _BLOCKED_OUTCOMES:
                blocked += 1
                continue
            claimed += 1
            if outcome == "succeeded":
                succeeded += 1
            elif outcome == "quarantined":
                quarantined += 1
            else:
                retryable += 1

    if dry_run:
        execution_status = "dry_run"
    elif blockers and (succeeded or retryable or quarantined):
        execution_status = "partial_failure"
    elif blockers:
        execution_status = "blocked"
    else:
        execution_status = "executed"

    return {
        "schema_version": "agent_knowledge_rag_ingress_delivery_drain.v1",
        "dry_run": bool(dry_run),
        "selected_count": len(selected),
        "payload_available_count": payload_available,
        "payload_missing_count": payload_missing,
        "payload_hash_mismatch_count": payload_hash_mismatch,
        "claimed_count": claimed,
        "executed_count": executed,
        "succeeded_count": succeeded,
        "retryable_count": retryable,
        "quarantined_count": quarantined,
        "blocked_count": blocked,
        "runtime_exceeded": runtime_exceeded,
        "blockers": blockers,
        "execution_status": execution_status,
        "mutation_performed": bool(executed),
        "network_used": bool(not dry_run and executed),
        "raw_ids_printed": False,
        "raw_paths_printed": False,
    }
