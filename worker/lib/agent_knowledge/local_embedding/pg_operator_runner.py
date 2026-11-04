"""Phase-separated operator runners for PostgreSQL local embeddings."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .operator_runner import (
    BackfillPlan,
    PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES,
    _base_report,
    _canonical_fingerprint,
    _counted_embedder,
)


def run_pg_backfill_phase(
    plan: BackfillPlan,
    *,
    store: Any,
    embedder: Any | None = None,
    couchdb_store: Any | None = None,
    checkpoint_path: Path | str | None = None,
) -> dict[str, Any]:
    """Dispatch one approved PG phase without constructing the other phase's clients."""
    if plan.lane != "pg":
        raise ValueError("pg_lane_required")
    if plan.phase == "vectors":
        if embedder is None or couchdb_store is not None or checkpoint_path is not None:
            raise ValueError("vectors_phase_clients_invalid")
        return run_pg_vectors_backfill(plan, store=store, embedder=embedder)
    if plan.phase == "receipts":
        if embedder is not None or couchdb_store is None or checkpoint_path is None:
            raise ValueError("receipts_phase_clients_invalid")
        return run_pg_receipts_backfill(
            plan,
            store=store,
            couchdb_store=couchdb_store,
            checkpoint_path=checkpoint_path,
        )
    raise ValueError("pg_phase_required")


def run_pg_vectors_backfill(
    plan: BackfillPlan,
    *,
    store: Any,
    embedder: Any,
) -> dict[str, Any]:
    """Enqueue/consume PG vectors only; CouchDB receipt writes are disabled."""
    if (
        plan.lane != "pg"
        or plan.phase != "vectors"
        or plan.vector_scope not in {"cards", "sessions", "all"}
    ):
        raise ValueError("vectors_phase_required")
    from . import transition

    started = time.monotonic()
    counted = _counted_embedder(embedder)
    counted.set_max_input_bytes(PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES)
    progress = transition.backfill_pg_local_embeddings(
        store,
        counted,
        profile_id=plan.profile_id,
        batch_size=plan.batch_size,
        max_batches=plan.max_batches,
        enqueue_limit_per_kind=plan.batch_size * plan.max_batches,
        lease_seconds=plan.lease_seconds,
        couchdb_store=None,
        vector_scope=plan.vector_scope,
        max_seconds=plan.max_seconds,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if plan.vector_scope == "cards":
        eligible = progress.eligible_cards
        selected = progress.enqueued_cards
        ready = progress.ready_cards
        missing = progress.missing_cards
    elif plan.vector_scope == "sessions":
        eligible = progress.eligible_sessions
        selected = progress.enqueued_sessions
        ready = progress.ready_sessions
        missing = progress.missing_sessions
    else:
        eligible = progress.eligible_cards + progress.eligible_sessions
        selected = progress.enqueued_cards + progress.enqueued_sessions
        ready = progress.ready_cards + progress.ready_sessions
        missing = progress.missing_cards + progress.missing_sessions
    attempted = progress.claimed
    outbox = {
        "queued": progress.outbox_queued,
        "processing": progress.outbox_processing,
        "failed": progress.outbox_failed_retryable,
        "dead_letter": progress.outbox_dead_letter,
    }
    outbox_ok = not any(outbox.values())
    success = missing == 0 and outbox_ok and progress.stale_fenced == 0
    if progress.outbox_dead_letter:
        stop_reason = "outbox_dead_letter"
    elif progress.outbox_processing:
        stop_reason = "outbox_processing"
    elif progress.outbox_failed_retryable:
        stop_reason = "outbox_failed"
    elif progress.outbox_queued:
        stop_reason = "outbox_queued"
    elif progress.stale_fenced:
        stop_reason = "stale_fenced"
    elif missing:
        stop_reason = "backlog_remaining"
    else:
        stop_reason = "completed"

    try:
        source_identity, source_count = transition._receipt_source_input_identity(
            store,
            profile_id=plan.profile_id,
            page_size=plan.receipt_page_size,
        )
    except Exception:
        source_identity, source_count = "", 0

    report = _base_report(plan, status="completed" if success else "partial")
    report.update(
        {
            "counts_available": True,
            "eligible": eligible,
            "selected": selected,
            "attempted": attempted,
            "ready": ready,
            "missing": missing,
            "receipt_source_snapshot_identity": source_identity,
            "receipt_source_snapshot_count": source_count,
            "receipt": {"status": "not_run", "writes": 0},
            "outbox": outbox,
            "elapsed_ms": elapsed_ms,
            "stop_reason": stop_reason,
            "network_used": True,
            "mutation_performed": True,
            "model_calls": counted.model_calls,
            "writer_concurrency_policy": "outbox_reconcile_on_next_run",
            "success": success,
        }
    )
    return report


def run_pg_receipts_backfill(
    plan: BackfillPlan,
    *,
    store: Any,
    couchdb_store: Any,
    checkpoint_path: Path | str,
) -> dict[str, Any]:
    """Process only valid ready-session receipts under the approved PG snapshot."""
    if plan.lane != "pg" or plan.phase != "receipts":
        raise ValueError("receipts_phase_required")
    from . import transition

    started = time.monotonic()
    receipt_budget = min(plan.receipt_max_seconds, plan.max_seconds)
    before = transition.inspect_pg_local_embedding_state(
        store,
        profile_id=plan.profile_id,
        couchdb_store=couchdb_store,
        receipt_page_size=plan.receipt_page_size,
        receipt_max_rows=plan.receipt_max_rows,
        receipt_max_seconds=receipt_budget,
    )
    progress = transition.process_pg_local_embedding_receipts(
        store,
        couchdb_store,
        profile_id=plan.profile_id,
        checkpoint_path=checkpoint_path,
        page_size=plan.receipt_page_size,
        max_pages=plan.receipt_max_pages,
        max_rows=plan.receipt_max_rows,
        max_seconds=receipt_budget,
        run_id=plan.receipt_run_id,
        expected_source_identity=plan.receipt_source_identity,
        target_fingerprint=plan.target_fingerprint,
    )
    after = transition.inspect_pg_local_embedding_state(
        store,
        profile_id=plan.profile_id,
        couchdb_store=couchdb_store,
        receipt_page_size=plan.receipt_page_size,
        receipt_max_rows=plan.receipt_max_rows,
        receipt_max_seconds=receipt_budget,
    )
    receipt_conflict = progress.conflict + after.conflicting_session_receipts
    receipt_failed = progress.failed + after.failed_session_receipts
    receipt = {
        "eligible": after.eligible_session_receipts,
        "checked": after.session_receipts_checked,
        "verified": after.session_receipts_verified,
        "ready": after.ready_session_receipts,
        "missing": after.missing_session_receipts,
        "unchecked": after.unchecked_session_receipts,
        "created": progress.created,
        "already_valid": progress.already_valid,
        "conflict": receipt_conflict,
        "failed": receipt_failed,
        "mutation_unknown": progress.mutation_unknown,
        "stale_skipped": progress.stale_skipped,
        "rows_processed": progress.rows_processed,
        "pages_processed": progress.pages_processed,
        "complete": progress.complete,
    }
    outbox = {
        "queued": after.outbox_queued,
        "processing": after.outbox_processing,
        "failed": after.outbox_failed_retryable,
        "dead_letter": after.outbox_dead_letter,
    }
    success = (
        after.session_receipts_verified
        and after.missing_session_receipts == 0
        and after.unchecked_session_receipts == 0
        and receipt_conflict == 0
        and receipt_failed == 0
        and progress.mutation_unknown == 0
        and progress.stale_skipped == 0
        and progress.complete
    )
    reason_counts = dict(progress.reason_counts)
    if reason_counts:
        stop_reason = sorted(reason_counts)[0]
    elif progress.mutation_unknown:
        stop_reason = "receipt_mutation_unknown"
    elif progress.stale_skipped:
        stop_reason = "receipt_stale_skipped"
    elif receipt_conflict:
        stop_reason = "receipt_conflict"
    elif receipt_failed:
        stop_reason = "receipt_failed"
    elif after.unchecked_session_receipts or not after.session_receipts_verified:
        stop_reason = "receipt_unverified"
    elif after.missing_session_receipts:
        stop_reason = "receipt_missing"
    elif not progress.complete:
        stop_reason = "receipt_sweep_incomplete"
    else:
        stop_reason = "completed"

    report = _base_report(plan, status="completed" if success else "partial")
    report.update(
        {
            "counts_available": True,
            "eligible": after.eligible_sessions,
            "selected": progress.rows_processed,
            "attempted": (
                progress.created
                + progress.already_valid
                + progress.conflict
                + progress.failed
                + progress.mutation_unknown
                + progress.stale_skipped
            ),
            "ready": after.ready_sessions,
            "missing": after.missing_sessions,
            "receipt": receipt,
            "inspection": {
                "before": _receipt_inspection_payload(before),
                "after": _receipt_inspection_payload(after),
            },
            "outbox": outbox,
            "receipt_run_id": plan.receipt_run_id,
            "receipt_source_snapshot_identity": plan.receipt_source_identity,
            "receipt_checkpoint_fingerprint": _canonical_fingerprint(
                {"path": plan.receipt_checkpoint_path}
            ),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "stop_reason": stop_reason,
            "network_used": True,
            "mutation_performed": True,
            "model_calls": 0,
            "writer_concurrency_policy": "fail_closed_approved_snapshot",
            "reason_counts": reason_counts,
            "success": success,
        }
    )
    return report


def _receipt_inspection_payload(progress: Any) -> dict[str, Any]:
    return {
        "eligible": progress.eligible_session_receipts,
        "checked": progress.session_receipts_checked,
        "verified": progress.session_receipts_verified,
        "ready": progress.ready_session_receipts,
        "missing": progress.missing_session_receipts,
        "unchecked": progress.unchecked_session_receipts,
        "conflict": progress.conflicting_session_receipts,
        "failed": progress.failed_session_receipts,
        "outbox": {
            "queued": progress.outbox_queued,
            "processing": progress.outbox_processing,
            "failed": progress.outbox_failed_retryable,
            "dead_letter": progress.outbox_dead_letter,
        },
    }
