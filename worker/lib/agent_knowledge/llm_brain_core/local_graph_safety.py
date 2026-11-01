"""Graphiti/Neo4j local Model2Vec concurrency, CAS, tombstone, and journal recovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable
import uuid

from graphiti_core.local_profile import (
    EDGE_LOCAL_EMBEDDING_PROP,
    EDGE_LOCAL_INPUT_HASH_PROP,
    EDGE_LOCAL_PROFILE_PROP,
    GRAPH_EMBEDDING_DIRTY_PROP,
    LOCAL_GRAPH_PROFILE_ID,
    LOCAL_GRAPH_VECTOR_DIM,
    NODE_LOCAL_EMBEDDING_PROP,
    NODE_LOCAL_INPUT_HASH_PROP,
    NODE_LOCAL_PROFILE_PROP,
    compute_graph_edge_input_hash,
    compute_graph_node_input_hash,
)

GRAPH_OPERATION_STEPS: tuple[str, ...] = (
    "extract_resolved",
    "entities_upserted",
    "edges_upserted",
    "invalidations_applied",
    "embeddings_committed",
)


class GroupFenceError(RuntimeError):
    """Base error for EmbeddingGroupFence violations."""


class GroupFenceBusyError(GroupFenceError):
    """Raised when another active lease holds the EmbeddingGroupFence for a group_id."""


class GroupQuarantinedError(GroupFenceError):
    """Raised when the group_id is quarantined due to a journal/effect_hash conflict."""


class GraphOperationConflictQuarantinedError(GroupFenceError):
    """Raised when a replayed graph step has a conflicting effect_hash."""


@dataclass(frozen=True)
class GroupLease:
    group_id: str
    epoch: int
    lease_owner: str
    lease_until: str
    active_operation_id: str
    status: str


@dataclass(frozen=True)
class GraphCasResult:
    applied: bool
    reason: str
    target_type: str
    target_uuid: str
    group_id: str
    epoch: int | None = None


def compute_step_effect_hash(payload: Any) -> str:
    """Compute deterministic sha256:<hex> effect hash for a graph operation step."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_neo4j_safety_schema(session: Any) -> None:
    """Create idempotent Neo4j constraints/indexes for local graph safety nodes."""
    queries = (
        "CREATE CONSTRAINT embedding_group_fence_group_id IF NOT EXISTS "
        "FOR (f:EmbeddingGroupFence) REQUIRE f.group_id IS UNIQUE",
        "CREATE INDEX embedding_deletion_target_idx IF NOT EXISTS "
        "FOR (d:EmbeddingDeletion) ON (d.target_type, d.target_uuid)",
        "CREATE INDEX graph_op_step_receipt_idx IF NOT EXISTS "
        "FOR (r:GraphOperationStepReceipt) ON (r.operation_id, r.step_id)",
    )
    for query in queries:
        session.run(query).consume()


def get_group_fence_state(session: Any, group_id: str) -> dict[str, Any]:
    """Return current EmbeddingGroupFence state for group_id (creating idle epoch 0 if absent)."""
    rec = session.run(
        """
        MERGE (f:EmbeddingGroupFence {group_id: $group_id})
        ON CREATE SET f.epoch = 0,
                      f.status = 'idle',
                      f.lease_owner = null,
                      f.lease_until = null,
                      f.active_operation_id = null,
                      f.updated_at = datetime()
        RETURN f.group_id AS group_id,
               coalesce(f.epoch, 0) AS epoch,
               coalesce(f.status, 'idle') AS status,
               f.lease_owner AS lease_owner,
               toString(f.lease_until) AS lease_until,
               f.active_operation_id AS active_operation_id,
               f.quarantine_reason AS quarantine_reason
        """,
        group_id=group_id,
    ).single()
    if rec is None:
        return {
            "group_id": group_id,
            "epoch": 0,
            "status": "idle",
            "lease_owner": None,
            "lease_until": None,
            "active_operation_id": None,
            "quarantine_reason": None,
        }
    return dict(rec)


def acquire_group_fence_lease(
    session: Any,
    group_id: str,
    *,
    lease_owner: str,
    operation_id: str,
    status: str = "backfill_in_progress",
    lease_seconds: int = 120,
    bump_epoch: bool = False,
) -> GroupLease:
    """Atomically acquire or renew an EmbeddingGroupFence lease in Neo4j."""
    if status not in {
        "live_write_in_progress",
        "backfill_in_progress",
        "reconciling",
    }:
        raise ValueError(f"Invalid active lease status: {status}")

    now_dt = datetime.now(timezone.utc)
    until_dt = now_dt + timedelta(seconds=max(1, int(lease_seconds)))
    now_iso = now_dt.isoformat()
    until_iso = until_dt.isoformat()

    rec = session.run(
        """
        MERGE (f:EmbeddingGroupFence {group_id: $group_id})
        ON CREATE SET f.epoch = 0,
                      f.status = 'idle',
                      f.lease_owner = null,
                      f.lease_until = null,
                      f.active_operation_id = null
        SET f._transient_cas_lock = $lock_token
        REMOVE f._transient_cas_lock
        WITH f,
             CASE
                 WHEN coalesce(f.status, 'idle') = 'quarantined' THEN 'quarantined'
                 WHEN f.lease_owner IS NULL
                      OR f.lease_owner = $lease_owner
                      OR f.lease_until IS NULL
                      OR f.lease_until <= datetime($now_iso)
                      OR coalesce(f.status, 'idle') = 'idle'
                 THEN 'acquired'
                 ELSE 'busy'
             END AS outcome
        FOREACH (_ IN CASE WHEN outcome = 'acquired' THEN [1] ELSE [] END |
            SET f.status = $status,
                f.lease_owner = $lease_owner,
                f.lease_until = datetime($until_iso),
                f.active_operation_id = $operation_id,
                f.epoch = coalesce(f.epoch, 0) + CASE WHEN $bump_epoch THEN 1 ELSE 0 END,
                f.updated_at = datetime($now_iso)
        )
        RETURN outcome,
               coalesce(f.epoch, 0) AS epoch,
               f.status AS current_status,
               f.lease_owner AS current_owner,
               toString(f.lease_until) AS current_until,
               f.active_operation_id AS current_op,
               f.quarantine_reason AS quarantine_reason
        """,
        group_id=group_id,
        lease_owner=lease_owner,
        operation_id=operation_id,
        status=status,
        now_iso=now_iso,
        until_iso=until_iso,
        bump_epoch=bool(bump_epoch),
        lock_token=str(uuid.uuid4()),
    ).single()

    if rec is None:
        raise GroupFenceError(f"Failed to evaluate EmbeddingGroupFence for group_id={group_id}")
    outcome = str(rec["outcome"])
    if outcome == "quarantined":
        raise GroupQuarantinedError(
            f"group_id={group_id} is quarantined: {rec.get('quarantine_reason') or 'conflict'}"
        )
    if outcome != "acquired":
        raise GroupFenceBusyError(
            f"group_id={group_id} is busy (status={rec['current_status']}, "
            f"owner={rec['current_owner']}, op={rec['current_op']})"
        )

    return GroupLease(
        group_id=group_id,
        epoch=int(rec["epoch"]),
        lease_owner=lease_owner,
        lease_until=until_iso,
        active_operation_id=operation_id,
        status=status,
    )


def release_group_fence_lease(
    session: Any,
    lease: GroupLease,
    *,
    bump_epoch: bool = False,
) -> bool:
    """Release an EmbeddingGroupFence lease if still owned by lease.lease_owner."""
    rec = session.run(
        """
        MATCH (f:EmbeddingGroupFence {group_id: $group_id})
        SET f._transient_cas_lock = $lock_token
        REMOVE f._transient_cas_lock
        WITH f,
             (f.lease_owner = $lease_owner AND f.status <> 'quarantined') AS owned
        FOREACH (_ IN CASE WHEN owned THEN [1] ELSE [] END |
            SET f.status = 'idle',
                f.lease_owner = null,
                f.lease_until = null,
                f.active_operation_id = null,
                f.epoch = coalesce(f.epoch, 0) + CASE WHEN $bump_epoch THEN 1 ELSE 0 END,
                f.updated_at = datetime()
        )
        RETURN owned
        """,
        group_id=lease.group_id,
        lease_owner=lease.lease_owner,
        bump_epoch=bool(bump_epoch),
        lock_token=str(uuid.uuid4()),
    ).single()
    return bool(rec and rec["owned"])


def bump_group_fence_epoch(session: Any, group_id: str) -> int:
    """Increment EmbeddingGroupFence.epoch and return the new epoch."""
    rec = session.run(
        """
        MERGE (f:EmbeddingGroupFence {group_id: $group_id})
        ON CREATE SET f.epoch = 1, f.status = 'idle', f.updated_at = datetime()
        ON MATCH SET f.epoch = coalesce(f.epoch, 0) + 1, f.updated_at = datetime()
        RETURN f.epoch AS epoch
        """,
        group_id=group_id,
    ).single()
    return int(rec["epoch"]) if rec is not None else 1


def quarantine_group_fence(
    session: Any,
    group_id: str,
    *,
    operation_id: str,
    reason: str,
) -> None:
    """Mark EmbeddingGroupFence as quarantined so subsequent writes/backfills fail closed."""
    session.run(
        """
        MERGE (f:EmbeddingGroupFence {group_id: $group_id})
        SET f.status = 'quarantined',
            f.active_operation_id = $operation_id,
            f.quarantine_reason = $reason,
            f.epoch = coalesce(f.epoch, 0) + 1,
            f.updated_at = datetime()
        """,
        group_id=group_id,
        operation_id=operation_id,
        reason=reason,
    ).consume()


def record_embedding_deletion(
    session: Any,
    *,
    target_type: str,
    target_uuid: str,
    group_id: str,
    reason: str = "deleted",
) -> int:
    """Record an EmbeddingDeletion tombstone and increment EmbeddingGroupFence.epoch."""
    if target_type not in {"entity", "edge"}:
        raise ValueError(f"target_type must be 'entity' or 'edge', got {target_type!r}")
    rec = session.run(
        """
        MERGE (d:EmbeddingDeletion {target_type: $target_type, target_uuid: $target_uuid})
        SET d.group_id = $group_id,
            d.reason = $reason,
            d.deleted_at = datetime()
        MERGE (f:EmbeddingGroupFence {group_id: $group_id})
        ON CREATE SET f.epoch = 1, f.status = 'idle', f.updated_at = datetime()
        ON MATCH SET f.epoch = coalesce(f.epoch, 0) + 1, f.updated_at = datetime()
        RETURN f.epoch AS epoch
        """,
        target_type=target_type,
        target_uuid=target_uuid,
        group_id=group_id,
        reason=reason,
    ).single()
    return int(rec["epoch"]) if rec is not None else 1


async def record_embedding_deletion_tombstone(
    driver: Any,
    *,
    target_uuid: str,
    target_kind: str = "entity",
    group_id: str,
    reason: str = "deleted",
) -> int:
    """Async GraphDriver wrapper to record an EmbeddingDeletion tombstone and bump fence epoch."""
    target_type = "edge" if target_kind == "edge" else "entity"
    execute_query = getattr(driver, "execute_query", None)
    if not callable(execute_query):
        return 0
    records, _, _ = await execute_query(
        """
        MERGE (d:EmbeddingDeletion {target_type: $target_type, target_uuid: $target_uuid})
        SET d.group_id = $group_id,
            d.reason = $reason,
            d.deleted_at = datetime()
        MERGE (f:EmbeddingGroupFence {group_id: $group_id})
        ON CREATE SET f.epoch = 1, f.status = 'idle', f.updated_at = datetime()
        ON MATCH SET f.epoch = coalesce(f.epoch, 0) + 1, f.updated_at = datetime()
        RETURN f.epoch AS epoch
        """,
        target_type=target_type,
        target_uuid=target_uuid,
        group_id=group_id,
        reason=reason,
    )
    if records:
        row = dict(records[0])
        return int(row.get("epoch") or 1)
    return 1


def cas_write_entity_local_embedding(
    session: Any,
    *,
    entity_uuid: str,
    group_id: str,
    expected_name: str,
    expected_input_hash: str | None = None,
    expected_epoch: int,
    vector: list[float],
    lease_owner: str | None = None,
) -> GraphCasResult:
    """Perform lock-before-read CAS write of `name_embedding_model2vec_v1` on an Entity node."""
    if len(vector) != LOCAL_GRAPH_VECTOR_DIM:
        raise ValueError(
            f"Expected {LOCAL_GRAPH_VECTOR_DIM}-dim vector, got {len(vector)}"
        )
    input_hash = expected_input_hash or compute_graph_node_input_hash(expected_name)
    rec = session.run(
        f"""
        OPTIONAL MATCH (f:EmbeddingGroupFence {{group_id: $group_id}})
        WITH f
        CALL (f) {{
            WITH f
            WHERE f IS NOT NULL
            SET f._transient_cas_lock = $lock_token
            REMOVE f._transient_cas_lock
            RETURN f.epoch AS fence_epoch,
                   coalesce(f.status, 'idle') AS fence_status,
                   f.lease_owner AS fence_owner
        }}
        OPTIONAL MATCH (d:EmbeddingDeletion {{target_type: 'entity', target_uuid: $entity_uuid}})
        OPTIONAL MATCH (n:Entity {{uuid: $entity_uuid}})
        FOREACH (_ IN CASE WHEN n IS NOT NULL THEN [1] ELSE [] END |
            SET n._transient_cas_lock = $lock_token
            REMOVE n._transient_cas_lock
        )
        WITH f, fence_epoch, fence_status, fence_owner, d, n,
             CASE
                 WHEN d IS NOT NULL THEN 'deleted_tombstone'
                 WHEN n IS NULL THEN 'target_missing'
                 WHEN f IS NULL THEN 'fence_missing'
                 WHEN fence_status = 'quarantined' THEN 'group_quarantined'
                 WHEN fence_epoch <> $expected_epoch THEN 'epoch_mismatch'
                 WHEN $lease_owner IS NOT NULL AND fence_owner IS NOT NULL AND fence_owner <> $lease_owner THEN 'lease_mismatch'
                 WHEN n.group_id <> $group_id THEN 'group_mismatch'
                 WHEN n.name <> $expected_name THEN 'text_changed'
                 WHEN n.{NODE_LOCAL_INPUT_HASH_PROP} IS NOT NULL
                      AND n.{NODE_LOCAL_INPUT_HASH_PROP} <> $input_hash THEN 'input_hash_changed'
                 ELSE 'ok'
             END AS reason
        FOREACH (_ IN CASE WHEN reason = 'ok' THEN [1] ELSE [] END |
            SET n.{NODE_LOCAL_EMBEDDING_PROP} = $vector,
                n.{NODE_LOCAL_PROFILE_PROP} = '{LOCAL_GRAPH_PROFILE_ID}',
                n.{NODE_LOCAL_INPUT_HASH_PROP} = $input_hash,
                n.{GRAPH_EMBEDDING_DIRTY_PROP} = false
        )
        RETURN reason, fence_epoch
        """,
        entity_uuid=entity_uuid,
        group_id=group_id,
        expected_name=expected_name,
        input_hash=input_hash,
        expected_epoch=int(expected_epoch),
        vector=[float(x) for x in vector],
        lease_owner=lease_owner,
        lock_token=str(uuid.uuid4()),
    ).single()

    if rec is None:
        return GraphCasResult(
            applied=False,
            reason="fence_missing",
            target_type="entity",
            target_uuid=entity_uuid,
            group_id=group_id,
            epoch=None,
        )
    reason = str(rec["reason"])
    return GraphCasResult(
        applied=(reason == "ok"),
        reason=reason,
        target_type="entity",
        target_uuid=entity_uuid,
        group_id=group_id,
        epoch=int(rec["fence_epoch"]) if rec["fence_epoch"] is not None else None,
    )


def cas_write_edge_local_embedding(
    session: Any,
    *,
    edge_uuid: str,
    group_id: str,
    expected_fact: str,
    expected_input_hash: str | None = None,
    expected_epoch: int,
    vector: list[float],
    lease_owner: str | None = None,
) -> GraphCasResult:
    """Perform lock-before-read CAS write of `fact_embedding_model2vec_v1` on a RELATES_TO edge."""
    if len(vector) != LOCAL_GRAPH_VECTOR_DIM:
        raise ValueError(
            f"Expected {LOCAL_GRAPH_VECTOR_DIM}-dim vector, got {len(vector)}"
        )
    input_hash = expected_input_hash or compute_graph_edge_input_hash(expected_fact)
    rec = session.run(
        f"""
        OPTIONAL MATCH (f:EmbeddingGroupFence {{group_id: $group_id}})
        WITH f
        CALL (f) {{
            WITH f
            WHERE f IS NOT NULL
            SET f._transient_cas_lock = $lock_token
            REMOVE f._transient_cas_lock
            RETURN f.epoch AS fence_epoch,
                   coalesce(f.status, 'idle') AS fence_status,
                   f.lease_owner AS fence_owner
        }}
        OPTIONAL MATCH (d:EmbeddingDeletion {{target_type: 'edge', target_uuid: $edge_uuid}})
        OPTIONAL MATCH (s:Entity)-[e:RELATES_TO {{uuid: $edge_uuid}}]->(t:Entity)
        FOREACH (_ IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END |
            SET e._transient_cas_lock = $lock_token
            REMOVE e._transient_cas_lock
        )
        WITH f, fence_epoch, fence_status, fence_owner, d, e,
             CASE
                 WHEN d IS NOT NULL THEN 'deleted_tombstone'
                 WHEN e IS NULL THEN 'target_missing'
                 WHEN f IS NULL THEN 'fence_missing'
                 WHEN fence_status = 'quarantined' THEN 'group_quarantined'
                 WHEN fence_epoch <> $expected_epoch THEN 'epoch_mismatch'
                 WHEN $lease_owner IS NOT NULL AND fence_owner IS NOT NULL AND fence_owner <> $lease_owner THEN 'lease_mismatch'
                 WHEN e.group_id <> $group_id THEN 'group_mismatch'
                 WHEN e.fact <> $expected_fact THEN 'text_changed'
                 WHEN e.{EDGE_LOCAL_INPUT_HASH_PROP} IS NOT NULL
                      AND e.{EDGE_LOCAL_INPUT_HASH_PROP} <> $input_hash THEN 'input_hash_changed'
                 ELSE 'ok'
             END AS reason
        FOREACH (_ IN CASE WHEN reason = 'ok' THEN [1] ELSE [] END |
            SET e.{EDGE_LOCAL_EMBEDDING_PROP} = $vector,
                e.{EDGE_LOCAL_PROFILE_PROP} = '{LOCAL_GRAPH_PROFILE_ID}',
                e.{EDGE_LOCAL_INPUT_HASH_PROP} = $input_hash,
                e.{GRAPH_EMBEDDING_DIRTY_PROP} = false
        )
        RETURN reason, fence_epoch
        """,
        edge_uuid=edge_uuid,
        group_id=group_id,
        expected_fact=expected_fact,
        input_hash=input_hash,
        expected_epoch=int(expected_epoch),
        vector=[float(x) for x in vector],
        lease_owner=lease_owner,
        lock_token=str(uuid.uuid4()),
    ).single()

    if rec is None:
        return GraphCasResult(
            applied=False,
            reason="fence_missing",
            target_type="edge",
            target_uuid=edge_uuid,
            group_id=group_id,
            epoch=None,
        )
    reason = str(rec["reason"])
    return GraphCasResult(
        applied=(reason == "ok"),
        reason=reason,
        target_type="edge",
        target_uuid=edge_uuid,
        group_id=group_id,
        epoch=int(rec["fence_epoch"]) if rec["fence_epoch"] is not None else None,
    )


class GraphOperationJournalCoordinator:
    """Coordinates durable graph operation checkpoints across PG `graph_operation_journal` and Neo4j."""

    def __init__(self, *, pg_store: Any | None = None) -> None:
        self._pg_store = pg_store

    def begin_operation(
        self,
        session: Any,
        *,
        operation_id: str,
        group_id: str,
        operation_type: str,
        lease_owner: str,
        lease_seconds: int = 120,
        fence_status: str = "live_write_in_progress",
        payload: dict[str, Any] | None = None,
    ) -> GroupLease:
        """Acquire Neo4j EmbeddingGroupFence lease and record/resume PG journal entry."""
        ensure_neo4j_safety_schema(session)
        lease = acquire_group_fence_lease(
            session,
            group_id,
            lease_owner=lease_owner,
            operation_id=operation_id,
            status=fence_status,
            lease_seconds=lease_seconds,
            bump_epoch=False,
        )
        if self._pg_store is not None:
            existing = self._pg_store.get_graph_operation_journal(operation_id)
            if existing is not None and existing.get("status") == "quarantined":
                quarantine_group_fence(
                    session,
                    group_id,
                    operation_id=operation_id,
                    reason=str(existing.get("quarantine_reason") or "pg_journal_quarantined"),
                )
                raise GroupQuarantinedError(
                    f"Operation {operation_id} is quarantined in graph_operation_journal"
                )
            self._pg_store.upsert_graph_operation_journal(
                operation_id=operation_id,
                group_id=group_id,
                operation_type=operation_type,
                status="in_progress",
                step_cursor=(existing.get("step_cursor") if existing else "init") or "init",
                expected_epoch=lease.epoch,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                payload=payload or (existing.get("payload_json") if existing else {}),
            )
        return lease

    def run_step(
        self,
        session: Any,
        *,
        operation_id: str,
        group_id: str,
        step_id: str,
        effect_payload: Any,
        action: Callable[[], Any],
    ) -> tuple[str, Any]:
        """Execute or replay a checkpointed step with `(operation_id, step_id, effect_hash)` verification.

        Returns `(outcome, result)` where `outcome` is `'executed'` or `'noop'`.
        Raises `GraphOperationConflictQuarantinedError` if a prior receipt for `(operation_id, step_id)`
        has a mismatched `effect_hash`.
        """
        if step_id not in GRAPH_OPERATION_STEPS:
            raise ValueError(f"Unsupported graph operation step_id: {step_id!r}")

        effect_hash = compute_step_effect_hash(effect_payload)

        # Check PG journal receipt first if configured
        pg_existing_hash: str | None = None
        if self._pg_store is not None:
            journal = self._pg_store.get_graph_operation_journal(operation_id)
            if journal is not None:
                if journal.get("status") == "quarantined":
                    raise GroupQuarantinedError(
                        f"Operation {operation_id} is already quarantined: "
                        f"{journal.get('quarantine_reason')}"
                    )
                receipts = journal.get("step_receipts_json") or {}
                if isinstance(receipts, dict) and step_id in receipts:
                    entry = receipts[step_id]
                    if isinstance(entry, dict):
                        pg_existing_hash = str(entry.get("effect_hash") or "")
                    elif isinstance(entry, str):
                        pg_existing_hash = entry

        # Check Neo4j receipt
        neo_rec = session.run(
            """
            OPTIONAL MATCH (r:GraphOperationStepReceipt {operation_id: $operation_id, step_id: $step_id})
            RETURN r.effect_hash AS effect_hash
            """,
            operation_id=operation_id,
            step_id=step_id,
        ).single()
        neo_existing_hash = (
            str(neo_rec["effect_hash"])
            if neo_rec is not None and neo_rec["effect_hash"] is not None
            else None
        )

        for recorded_hash in (pg_existing_hash, neo_existing_hash):
            if recorded_hash:
                if recorded_hash != effect_hash:
                    reason = (
                        f"step={step_id} effect_hash mismatch: "
                        f"recorded={recorded_hash} vs current={effect_hash}"
                    )
                    quarantine_group_fence(
                        session,
                        group_id,
                        operation_id=operation_id,
                        reason=reason,
                    )
                    if self._pg_store is not None:
                        self._pg_store.record_graph_operation_step(
                            operation_id=operation_id,
                            step_id=step_id,
                            effect_hash=effect_hash,
                        )
                    raise GraphOperationConflictQuarantinedError(reason)

        if pg_existing_hash == effect_hash and neo_existing_hash == effect_hash:
            return "noop", None
        if neo_existing_hash == effect_hash and self._pg_store is None:
            return "noop", None

        result = action()

        session.run(
            """
            MERGE (r:GraphOperationStepReceipt {operation_id: $operation_id, step_id: $step_id})
            ON CREATE SET r.group_id = $group_id,
                          r.effect_hash = $effect_hash,
                          r.recorded_at = datetime()
            ON MATCH SET r.group_id = $group_id,
                         r.effect_hash = $effect_hash,
                         r.updated_at = datetime()
            """,
            operation_id=operation_id,
            step_id=step_id,
            group_id=group_id,
            effect_hash=effect_hash,
        ).consume()

        if self._pg_store is not None:
            step_outcome = self._pg_store.record_graph_operation_step(
                operation_id=operation_id,
                step_id=step_id,
                effect_hash=effect_hash,
                completed=(step_id == "embeddings_committed"),
            )
            if step_outcome == "quarantined":
                reason = f"step={step_id} quarantined by PG journal effect_hash conflict"
                quarantine_group_fence(
                    session,
                    group_id,
                    operation_id=operation_id,
                    reason=reason,
                )
                raise GraphOperationConflictQuarantinedError(reason)

        return "executed", result

    def complete_operation(
        self,
        session: Any,
        *,
        lease: GroupLease,
        bump_epoch: bool = False,
    ) -> None:
        """Mark operation completed and release the Neo4j EmbeddingGroupFence lease."""
        release_group_fence_lease(session, lease, bump_epoch=bump_epoch)
