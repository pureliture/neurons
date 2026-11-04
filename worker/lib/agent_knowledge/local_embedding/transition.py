from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from graphiti_core.local_profile import (
    compute_graph_edge_input_hash,
    compute_graph_node_input_hash,
    format_graph_edge_embedding_input,
    format_graph_node_embedding_input,
)

from ..couchdb_source.document_model import projection_state_doc_id
from ..llm_brain_core.local_graph_safety import (
    GraphOperationConflictQuarantinedError,
    GraphOperationJournalCoordinator,
    GroupFenceBusyError,
    GroupQuarantinedError,
    acquire_group_fence_lease,
    cas_write_edge_local_embedding,
    cas_write_entity_local_embedding,
    ensure_neo4j_safety_schema,
    release_group_fence_lease,
)
from ..model_connectors.policy import validate_local_embedding_write_or_query_policy
from .canonical import (
    card_input_v1,
    compute_input_hash,
    session_input_v1,
)
from .profile import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL_ALIAS,
    LOCAL_EMBEDDING_PROFILE_ID,
)

PG_RECEIPT_CURSOR_SCHEMA_VERSION = "local_embedding_pg_receipt_cursor.v3"
PG_RECEIPT_PAGE_SIZE_MAX = 256
PG_RECEIPT_PAGES_MAX = 10
PG_RECEIPT_ROWS_MAX = 5000
PG_RECEIPT_SECONDS_MAX = 600.0
PG_RECEIPT_CAS_ATTEMPTS = 3
GRAPH_READBACK_PAGE_SIZE = 256


def _cell(row: Any, key: str, index: int) -> Any:
    """Read one column from a psycopg row regardless of row factory (tuple or dict_row)."""
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _scalar_count(row: Any) -> int:
    if row is None:
        return 0
    if isinstance(row, Mapping):
        return int(next(iter(row.values())) or 0)
    return int(row[0] or 0)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PgBackfillProgress:
    eligible_cards: int = 0
    ready_cards: int = 0
    missing_cards: int = 0
    eligible_sessions: int = 0
    ready_sessions: int = 0
    missing_sessions: int = 0
    enqueued_cards: int = 0
    enqueued_sessions: int = 0
    batches_executed: int = 0
    claimed: int = 0
    completed: int = 0
    stale_fenced: int = 0
    retried: int = 0
    quarantined: int = 0
    outbox_queued: int = 0
    outbox_processing: int = 0
    outbox_failed_retryable: int = 0
    outbox_dead_letter: int = 0
    tombstones_count: int = 0
    legacy_card_embeddings_preserved: int = 0
    legacy_session_embeddings_preserved: int = 0
    eligible_session_receipts: int = 0
    ready_session_receipts: int = 0
    missing_session_receipts: int = 0
    session_receipts_verified: bool = False
    session_receipts_checked: int = 0
    conflicting_session_receipts: int = 0
    failed_session_receipts: int = 0
    unchecked_session_receipts: int = 0
    session_receipts_verified_before: bool = False
    valid_session_receipts_before: int = 0
    missing_session_receipts_before: int = 0
    conflicting_session_receipts_before: int = 0
    failed_session_receipts_before: int = 0
    receipt_rows_processed: int = 0
    receipt_pages_processed: int = 0
    receipt_created: int = 0
    receipt_already_valid: int = 0
    receipt_conflict: int = 0
    receipt_failed: int = 0
    receipt_mutation_unknown: int = 0
    receipt_stale_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PgReceiptProgress:
    rows_processed: int = 0
    pages_processed: int = 0
    created: int = 0
    already_valid: int = 0
    conflict: int = 0
    failed: int = 0
    mutation_unknown: int = 0
    stale_skipped: int = 0
    complete: bool = False
    run_id: str = ""
    reason_counts: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReceiptWriteResult:
    status: str
    reason_code: str


class _Neo4jStaleBatchWrite(RuntimeError):
    """At least one batch CAS lost its source/fence precondition."""


class _Neo4jBatchReadbackIncomplete(RuntimeError):
    """A batch did not pass exact post-write identity readback."""


@dataclass(frozen=True)
class PgReceiptInspection:
    checked: int = 0
    valid: int = 0
    missing: int = 0
    conflict: int = 0
    failed: int = 0
    unchecked: int = 0
    verified: bool = False


@dataclass(frozen=True)
class Neo4jBackfillProgress:
    group_ids_scanned: tuple[str, ...] = ()
    eligible_entities: int = 0
    ready_entities: int = 0
    missing_or_dirty_entities: int = 0
    eligible_edges: int = 0
    ready_edges: int = 0
    missing_or_dirty_edges: int = 0
    entities_embedded: int = 0
    entities_fenced_stale: int = 0
    edges_embedded: int = 0
    edges_fenced_stale: int = 0
    groups_fenced_busy: int = 0
    groups_quarantined: int = 0
    groups_conflicted: int = 0
    batches_attempted: int = 0
    model_calls: int = 0
    stop_reason: str = "completed"
    tombstones_count: int = 0
    legacy_entity_embeddings_preserved: int = 0
    legacy_edge_embeddings_preserved: int = 0
    readback_complete: bool = False
    readback_verified: bool = False
    readback_input_hash_mismatches: int = 0
    readback_profile_mismatches: int = 0
    readback_invalid_vectors: int = 0
    readback_dirty_targets: int = 0
    readback_tombstoned_targets: int = 0
    readback_tombstoned_embeddings: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["group_ids_scanned"] = list(self.group_ids_scanned)
        return data


@dataclass(frozen=True)
class LocalEmbeddingCutoverReport:
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID
    checked_at: str = field(default_factory=_utc_now_iso)
    ready: bool = False
    blocking_reasons: tuple[str, ...] = ()
    pg: PgBackfillProgress | None = None
    neo4j: Neo4jBackfillProgress | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "checked_at": self.checked_at,
            "ready": self.ready,
            "blocking_reasons": list(self.blocking_reasons),
            "pg": self.pg.to_dict() if self.pg is not None else None,
            "neo4j": self.neo4j.to_dict() if self.neo4j is not None else None,
        }


def _cas_merge_projection_state_receipt(
    *,
    couchdb_store: Any,
    doc_id: str,
    profile_id: str,
    receipt: dict[str, Any],
    validator: Callable[[dict[str, Any]], bool],
    max_attempts: int = PG_RECEIPT_CAS_ATTEMPTS,
) -> ReceiptWriteResult:
    """Merge one profile receipt into the latest revision and verify by readback."""
    attempts = int(max_attempts)
    if not 1 <= attempts <= PG_RECEIPT_CAS_ATTEMPTS:
        raise ValueError("receipt CAS attempts exceed the hard bound")

    saw_revision_conflict = False
    for _attempt in range(attempts):
        try:
            current = couchdb_store.get(doc_id)
        except Exception:
            return ReceiptWriteResult("failed", "projection_state_read_failed")
        if not isinstance(current, dict):
            return ReceiptWriteResult("failed", "projection_state_missing")

        latest_receipts = current.get("backend_receipts")
        latest_local = (
            latest_receipts.get("postgres_pgvector_local")
            if isinstance(latest_receipts, dict)
            else None
        )
        latest_profile_receipt = (
            latest_local.get(profile_id) if isinstance(latest_local, dict) else None
        )
        if isinstance(latest_profile_receipt, dict):
            try:
                if latest_profile_receipt == receipt and validator(latest_profile_receipt):
                    return ReceiptWriteResult("already_valid", "receipt_already_valid")
            except Exception:
                return ReceiptWriteResult("failed", "receipt_readback_validation_failed")

        expected_rev = str(current.get("_rev") or "")
        if not expected_rev:
            return ReceiptWriteResult("failed", "projection_state_revision_missing")

        merged = copy.deepcopy(current)
        receipts = merged.get("backend_receipts")
        if not isinstance(receipts, dict):
            receipts = {}
        else:
            receipts = copy.deepcopy(receipts)
        local_map = receipts.get("postgres_pgvector_local")
        if not isinstance(local_map, dict):
            local_map = {}
        else:
            local_map = copy.deepcopy(local_map)
        local_map[profile_id] = copy.deepcopy(receipt)
        receipts["postgres_pgvector_local"] = local_map
        merged["backend_receipts"] = receipts

        write_error = False
        try:
            couchdb_store.put_if_revision(merged, expected_rev=expected_rev)
        except Exception:
            write_error = True

        try:
            readback = couchdb_store.get(doc_id)
        except Exception:
            return ReceiptWriteResult("mutation_unknown", "readback_unavailable")

        readback_receipts = readback.get("backend_receipts") if isinstance(readback, dict) else None
        readback_local = (
            readback_receipts.get("postgres_pgvector_local")
            if isinstance(readback_receipts, dict)
            else None
        )
        readback_receipt = (
            readback_local.get(profile_id) if isinstance(readback_local, dict) else None
        )
        if isinstance(readback_receipt, dict):
            try:
                if readback_receipt == receipt and validator(readback_receipt):
                    if write_error:
                        return ReceiptWriteResult(
                            "already_valid", "readback_valid_after_uncertain_write"
                        )
                    return ReceiptWriteResult("created", "receipt_created")
            except Exception:
                return ReceiptWriteResult("failed", "receipt_readback_validation_failed")

        readback_rev = str((readback or {}).get("_rev") or "") if isinstance(readback, dict) else ""
        if readback_rev and readback_rev != expected_rev:
            saw_revision_conflict = True
            continue
        if write_error:
            return ReceiptWriteResult("failed", "receipt_write_failed")
        if _attempt + 1 < attempts:
            continue
        return ReceiptWriteResult("conflict", "receipt_readback_mismatch")

    if saw_revision_conflict:
        return ReceiptWriteResult("conflict", "receipt_revision_conflict_exhausted")
    return ReceiptWriteResult("failed", "receipt_write_failed")


def _receipt_row_mapping(row: Any) -> dict[str, str]:
    names = (
        "session_id_hash",
        "chunk_id",
        "project",
        "provider",
        "content_hash",
        "input_hash",
    )
    return {name: str(_cell(row, name, index) or "") for index, name in enumerate(names)}


def _fetch_pg_local_receipt_page(
    store: Any,
    *,
    profile_id: str,
    after_key: tuple[str, str] | None,
    limit: int,
) -> list[dict[str, str]]:
    test_page_reader = getattr(store, "iter_local_embedding_receipt_rows", None)
    if callable(test_page_reader):
        rows = test_page_reader(after_key=after_key, limit=limit, profile_id=profile_id)
    else:
        after_clause = "" if after_key is None else "AND (smc.session_id_hash, smc.chunk_id) > (%s, %s)"
        query = f"""
            SELECT smc.session_id_hash, smc.chunk_id, smc.project, smc.provider,
                   smc.content_hash, sle.input_hash
              FROM session_memory_chunks smc
              JOIN session_local_embeddings sle
                ON sle.chunk_id = smc.chunk_id
               AND sle.profile_id = %s
               AND sle.source_content_hash = smc.content_hash
               AND sle.state = 'ready'
             WHERE TRUE {after_clause}
             ORDER BY smc.session_id_hash ASC, smc.chunk_id ASC
             LIMIT %s
        """
        params: tuple[Any, ...] = (profile_id, limit) if after_key is None else (
            profile_id,
            after_key[0],
            after_key[1],
            limit,
        )
        with store._scope(write=False) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall() or ()
    page = [_receipt_row_mapping(row) for row in (rows or ())]
    if len(page) > limit:
        raise ValueError("receipt source page exceeded its row ceiling")
    return page


def _receipt_source_input_identity(store: Any, *, profile_id: str, page_size: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    digest.update(profile_id.encode("utf-8"))
    digest.update(b"\n")
    after_key: tuple[str, str] | None = None
    count = 0
    while True:
        page = _fetch_pg_local_receipt_page(
            store,
            profile_id=profile_id,
            after_key=after_key,
            limit=page_size,
        )
        if not page:
            break
        for row in page:
            key = (row["session_id_hash"], row["chunk_id"])
            if not all(key) or (after_key is not None and key <= after_key):
                raise ValueError("receipt source keyset is invalid")
            digest.update(
                json.dumps(
                    [*key, row["content_hash"], row["input_hash"]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
            after_key = key
            count += 1
        if len(page) < page_size:
            break
    return "sha256:" + digest.hexdigest(), count


def _receipt_key_after_progress(
    store: Any,
    *,
    profile_id: str,
    page_size: int,
    rows_processed: int,
) -> tuple[str, str] | None:
    if rows_processed <= 0:
        return None
    after_key: tuple[str, str] | None = None
    observed = 0
    while observed < rows_processed:
        page = _fetch_pg_local_receipt_page(
            store,
            profile_id=profile_id,
            after_key=after_key,
            limit=min(page_size, rows_processed - observed),
        )
        if not page:
            raise ValueError("receipt checkpoint is beyond the current source")
        for row in page:
            after_key = (row["session_id_hash"], row["chunk_id"])
            observed += 1
            if observed == rows_processed:
                break
    return after_key


def _write_pg_receipt_checkpoint(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _receipt_run_result(
    *,
    manifest: dict[str, Any] | None,
    run_id: str,
    counts: Mapping[str, int],
    reasons: Mapping[str, int],
    complete: bool,
) -> PgReceiptProgress:
    progress = (manifest or {}).get("progress") or {}
    return PgReceiptProgress(
        rows_processed=int(progress.get("rows_processed") or 0),
        pages_processed=int(progress.get("pages_processed") or 0),
        created=int(counts.get("created", 0)),
        already_valid=int(counts.get("already_valid", 0)),
        conflict=int(counts.get("conflict", 0)),
        failed=int(counts.get("failed", 0)),
        mutation_unknown=int(counts.get("mutation_unknown", 0)),
        stale_skipped=int(counts.get("stale_skipped", 0)),
        complete=complete,
        run_id=run_id,
        reason_counts=dict(reasons),
    )


def process_pg_local_embedding_receipts(
    store: Any,
    couchdb_store: Any,
    *,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    checkpoint_path: Path | str | None = None,
    page_size: int = 64,
    max_pages: int = 10,
    max_rows: int = 500,
    max_seconds: float = 60,
    run_id: str | None = None,
    expected_source_identity: str | None = None,
    target_fingerprint: str | None = None,
) -> PgReceiptProgress:
    """Stream ready vectors with a budget checked between receipt pages.

    Source-identity and cursor-reconstruction scans, plus an in-progress page,
    are not covered by this budget.
    """
    page_size = int(page_size)
    max_pages = int(max_pages)
    max_rows = int(max_rows)
    max_seconds = float(max_seconds)
    if not 1 <= page_size <= PG_RECEIPT_PAGE_SIZE_MAX:
        raise ValueError("receipt page size exceeds the hard bound")
    if not 1 <= max_pages <= PG_RECEIPT_PAGES_MAX:
        raise ValueError("receipt page ceiling exceeds the hard bound")
    if not 1 <= max_rows <= PG_RECEIPT_ROWS_MAX:
        raise ValueError("receipt row ceiling exceeds the hard bound")
    if not 0 < max_seconds <= PG_RECEIPT_SECONDS_MAX:
        raise ValueError("receipt time ceiling exceeds the hard bound")

    checkpoint = Path(checkpoint_path) if checkpoint_path is not None else None
    existing: dict[str, Any] | None = None
    if checkpoint is not None and checkpoint.exists():
        try:
            loaded = json.loads(checkpoint.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return PgReceiptProgress(failed=1, reason_counts={"checkpoint_invalid": 1})
        if not isinstance(existing, dict):
            return PgReceiptProgress(failed=1, reason_counts={"checkpoint_invalid": 1})
        if (
            existing.get("schema_version") != PG_RECEIPT_CURSOR_SCHEMA_VERSION
            or existing.get("profile_id") != profile_id
            or existing.get("page_size") != page_size
            or existing.get("page_ceiling") != max_pages
            or existing.get("row_ceiling") != max_rows
            or existing.get("between_page_budget_seconds") != max_seconds
            or existing.get("budget_semantics") != "between_receipt_pages"
        ):
            return PgReceiptProgress(failed=1, reason_counts={"checkpoint_contract_mismatch": 1})

    resolved_run_id = str((existing or {}).get("run_id") or run_id or uuid.uuid4().hex)
    if run_id and existing and str(existing.get("run_id") or "") != str(run_id):
        return PgReceiptProgress(
            rows_processed=int((existing.get("progress") or {}).get("rows_processed") or 0),
            failed=1,
            run_id=resolved_run_id,
            reason_counts={"checkpoint_run_id_mismatch": 1},
        )
    if (
        target_fingerprint
        and existing
        and str(existing.get("target_fingerprint") or "") != target_fingerprint
    ):
        return PgReceiptProgress(
            rows_processed=int((existing.get("progress") or {}).get("rows_processed") or 0),
            failed=1,
            run_id=resolved_run_id,
            reason_counts={"checkpoint_target_mismatch": 1},
        )
    try:
        source_identity, source_count = _receipt_source_input_identity(
            store, profile_id=profile_id, page_size=page_size
        )
    except Exception:
        return PgReceiptProgress(failed=1, run_id=resolved_run_id, reason_counts={"source_scan_failed": 1})

    if expected_source_identity and source_identity != expected_source_identity:
        return PgReceiptProgress(
            rows_processed=int((existing or {}).get("progress", {}).get("rows_processed") or 0),
            failed=1,
            run_id=resolved_run_id,
            reason_counts={"approved_source_identity_mismatch": 1},
        )

    previous_manifest = copy.deepcopy(existing)
    if existing and existing.get("source_input_identity") != source_identity:
        return PgReceiptProgress(
            rows_processed=int((existing.get("progress") or {}).get("rows_processed") or 0),
            failed=1,
            run_id=resolved_run_id,
            reason_counts={"source_identity_changed": 1},
        )

    manifest = existing or {
        "schema_version": PG_RECEIPT_CURSOR_SCHEMA_VERSION,
        "profile_id": profile_id,
        "source_input_identity": source_identity,
        "target_fingerprint": target_fingerprint or "",
        "page_size": page_size,
        "page_ceiling": max_pages,
        "row_ceiling": max_rows,
        "between_page_budget_seconds": max_seconds,
        "budget_semantics": "between_receipt_pages",
        "run_id": resolved_run_id,
        "progress": {
            "rows_processed": 0,
            "pages_processed": 0,
            "last_key_fingerprint": "",
        },
    }
    if checkpoint is not None and existing is None:
        _write_pg_receipt_checkpoint(checkpoint, manifest)

    progress = manifest.get("progress") or {}
    rows_processed = int(progress.get("rows_processed") or 0)
    if rows_processed > source_count:
        return PgReceiptProgress(
            rows_processed=rows_processed,
            failed=1,
            run_id=resolved_run_id,
            reason_counts={"checkpoint_progress_invalid": 1},
        )
    try:
        last_key = _receipt_key_after_progress(
            store,
            profile_id=profile_id,
            page_size=page_size,
            rows_processed=rows_processed,
        )
    except Exception:
        return PgReceiptProgress(
            rows_processed=rows_processed,
            failed=1,
            run_id=resolved_run_id,
            reason_counts={"checkpoint_cursor_invalid": 1},
        )

    counts: dict[str, int] = {
        "created": 0,
        "already_valid": 0,
        "conflict": 0,
        "failed": 0,
        "mutation_unknown": 0,
        "stale_skipped": 0,
    }
    reasons: dict[str, int] = {}
    pages_this_run = 0
    rows_this_run = 0
    deadline = time.monotonic() + max_seconds
    complete = rows_processed >= source_count

    while pages_this_run < max_pages and rows_this_run < max_rows and time.monotonic() < deadline:
        page_limit = min(page_size, max_rows - rows_this_run)
        try:
            page = _fetch_pg_local_receipt_page(
                store,
                profile_id=profile_id,
                after_key=last_key,
                limit=page_limit,
            )
        except Exception:
            counts["failed"] += 1
            reasons["source_page_read_failed"] = reasons.get("source_page_read_failed", 0) + 1
            break
        if not page:
            complete = True
            break

        page_ok = True
        for row in page:
            try:
                status, reason_code = _process_pg_local_receipt_row(
                    store=store,
                    couchdb_store=couchdb_store,
                    row=row,
                    profile_id=profile_id,
                )
            except Exception:
                status, reason_code = "failed", "receipt_processing_error"
            if status not in counts:
                status, reason_code = "failed", "invalid_receipt_result"
            counts[status] += 1
            if reason_code:
                reasons[reason_code] = reasons.get(reason_code, 0) + 1
            if status in {"conflict", "failed", "mutation_unknown", "stale_skipped"}:
                page_ok = False
                break

        if not page_ok:
            break

        last_key = (page[-1]["session_id_hash"], page[-1]["chunk_id"])
        rows_processed += len(page)
        rows_this_run += len(page)
        pages_this_run += 1
        manifest["progress"] = {
            "rows_processed": rows_processed,
            "pages_processed": int(progress.get("pages_processed") or 0) + pages_this_run,
            "last_key_fingerprint": "sha256:" + hashlib.sha256(
                json.dumps(last_key, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        if checkpoint is not None:
            _write_pg_receipt_checkpoint(checkpoint, manifest)
        complete = rows_processed >= source_count or len(page) < page_limit
        if complete:
            break

    if pages_this_run:
        try:
            final_identity, final_count = _receipt_source_input_identity(
                store, profile_id=profile_id, page_size=page_size
            )
        except Exception:
            final_identity, final_count = "", -1
        if final_identity != source_identity or final_count != source_count:
            reasons["source_identity_changed"] = reasons.get("source_identity_changed", 0) + 1
            counts["failed"] += 1
            if checkpoint is not None:
                if previous_manifest is not None:
                    _write_pg_receipt_checkpoint(checkpoint, previous_manifest)
                else:
                    manifest["progress"] = {
                        "rows_processed": 0,
                        "pages_processed": 0,
                        "last_key_fingerprint": "",
                    }
                    _write_pg_receipt_checkpoint(checkpoint, manifest)
            manifest = previous_manifest or manifest
            complete = False

    return _receipt_run_result(
        manifest=manifest,
        run_id=resolved_run_id,
        counts=counts,
        reasons=reasons,
        complete=complete,
    )


def _process_pg_local_receipt_row(
    *,
    store: Any,
    couchdb_store: Any,
    row: Mapping[str, str],
    profile_id: str,
) -> tuple[str, str]:
    from ..couchdb_source.session_memory_materializer import materialize_session_memory
    from ..model_connectors import DEFAULT_EMBEDDING_PROFILE_ID
    from ..rag_ingress.pg_backfill import _derive_pg_chunk_id_impl
    from ..rag_ingress.pg_embedding_chunks import CHUNKED_PROFILE
    from ..rag_ingress.pg_representation import (
        LOCAL_SESSION_REPRESENTATION_KIND,
        build_local_representation_receipt_v3,
        derive_local_pg_chunk_id,
        valid_local_pg_receipt_metadata,
        valid_local_representation_receipt_v3,
    )

    chunk_id = str(row.get("chunk_id") or "")
    project = str(row.get("project") or "")
    provider = str(row.get("provider") or "unspecified")
    session_id_hash = str(row.get("session_id_hash") or "")
    content_hash = str(row.get("content_hash") or "")
    input_hash = str(row.get("input_hash") or "")
    if not all((chunk_id, project, session_id_hash, content_hash, input_hash)):
        return "failed", "receipt_source_identity_incomplete"

    try:
        current_materialized = materialize_session_memory(
            session_id_hash=session_id_hash,
            store=couchdb_store,
        )
    except Exception:
        return "failed", "materialization_failed"
    if current_materialized is None or not getattr(current_materialized, "fully_materialized", False):
        return "failed", "materialization_incomplete"

    active_content_hash = str(getattr(current_materialized, "content_hash", "") or "")
    source_hash = str(getattr(current_materialized, "source_hash", "") or "")
    if not active_content_hash or not source_hash:
        return "failed", "materialization_identity_missing"
    if content_hash != active_content_hash:
        return "stale_skipped", "inactive_session_chunk"

    try:
        local_v1_id = derive_local_pg_chunk_id(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
            source_hash=source_hash,
            content_hash=content_hash,
            embedding_profile=profile_id,
        )
        legacy_v1_id = _derive_pg_chunk_id_impl(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
            source_hash=source_hash,
            content_hash=content_hash,
            embedding_profile=DEFAULT_EMBEDDING_PROFILE_ID,
        )
        legacy_v2_id = _derive_pg_chunk_id_impl(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
            source_hash=source_hash,
            content_hash=content_hash,
            embedding_profile=CHUNKED_PROFILE,
        )
    except Exception:
        return "failed", "receipt_identity_derivation_failed"

    if chunk_id == local_v1_id:
        identity_scheme = "local_v1"
    elif chunk_id in (legacy_v1_id, legacy_v2_id):
        identity_scheme = "legacy_exact"
    else:
        identity_scheme = "legacy_reuse"

    try:
        receipt = build_local_representation_receipt_v3(
            representation_kind=LOCAL_SESSION_REPRESENTATION_KIND,
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
            active_content_hash=active_content_hash,
            projected_source_hash=source_hash,
            representation_content_hash=content_hash,
            session_memory_knowledge_id=chunk_id,
            identity_scheme=identity_scheme,
            embedding_profile=profile_id,
            input_hash=input_hash,
        )
        if not valid_local_representation_receipt_v3(
            receipt,
            store=store,
            current_materialized=current_materialized,
            require_ready_vector=True,
        ):
            return "failed", "receipt_vector_validation_failed"
        validator = lambda candidate: valid_local_representation_receipt_v3(
            candidate,
            store=store,
            current_materialized=current_materialized,
            require_ready_vector=True,
        )
    except Exception:
        return "failed", "receipt_validation_failed"

    result = _cas_merge_projection_state_receipt(
        couchdb_store=couchdb_store,
        doc_id=projection_state_doc_id(session_id_hash),
        profile_id=profile_id,
        receipt=receipt,
        validator=validator,
    )
    return result.status, result.reason_code


def _inspect_pg_session_receipts(
    store: Any,
    couchdb_store: Any,
    *,
    profile_id: str,
    eligible: int,
    page_size: int = 64,
    max_rows: int = PG_RECEIPT_ROWS_MAX,
    max_seconds: float = 60,
) -> PgReceiptInspection:
    from ..couchdb_source.session_memory_materializer import materialize_session_memory
    from ..rag_ingress.pg_representation import valid_local_pg_receipt_metadata

    checked = valid = missing = conflict = failed = 0
    after_session_hash = ""
    deadline = time.monotonic() + max_seconds
    while checked < min(eligible, max_rows) and time.monotonic() < deadline:
        reader = getattr(store, "iter_local_session_receipt_session_ids", None)
        try:
            if callable(reader):
                session_hashes = reader(after_session_hash=after_session_hash, limit=page_size)
            else:
                if after_session_hash:
                    query = (
                        "SELECT DISTINCT session_id_hash FROM session_memory_chunks "
                        "WHERE session_id_hash > %s ORDER BY session_id_hash ASC LIMIT %s"
                    )
                    params: tuple[Any, ...] = (after_session_hash, page_size)
                else:
                    query = (
                        "SELECT DISTINCT session_id_hash FROM session_memory_chunks "
                        "ORDER BY session_id_hash ASC LIMIT %s"
                    )
                    params = (page_size,)
                with store._scope(write=False) as conn:
                    with conn.cursor() as cur:
                        cur.execute(query, params)
                        session_hashes = [
                            str(_cell(row, "session_id_hash", 0) or "")
                            for row in (cur.fetchall() or ())
                        ]
        except Exception:
            failed += 1
            break

        if not session_hashes:
            break
        for session_hash_value in session_hashes:
            session_hash = str(session_hash_value or "")
            if not session_hash or (after_session_hash and session_hash <= after_session_hash):
                failed += 1
                break
            after_session_hash = session_hash
            checked += 1
            try:
                document = couchdb_store.get(projection_state_doc_id(session_hash))
            except Exception:
                failed += 1
                continue
            if not isinstance(document, dict):
                missing += 1
                continue
            receipts = document.get("backend_receipts")
            local_map = (
                receipts.get("postgres_pgvector_local")
                if isinstance(receipts, dict)
                else None
            )
            receipt = local_map.get(profile_id) if isinstance(local_map, dict) else None
            if not isinstance(receipt, dict) or receipt.get("receipt_version") != 3:
                missing += 1
                continue
            try:
                current_materialized = materialize_session_memory(
                    session_id_hash=session_hash,
                    store=couchdb_store,
                )
                if current_materialized is None or not getattr(
                    current_materialized, "fully_materialized", False
                ):
                    conflict += 1
                elif valid_local_pg_receipt_metadata(
                    receipt,
                    store=store,
                    current_materialized=current_materialized,
                    require_ready_vector=True,
                ):
                    valid += 1
                else:
                    conflict += 1
            except Exception:
                failed += 1
        if len(session_hashes) < page_size:
            break

    unchecked = max(0, eligible - checked)
    return PgReceiptInspection(
        checked=checked,
        valid=valid,
        missing=missing,
        conflict=conflict,
        failed=failed,
        unchecked=unchecked,
        verified=unchecked == 0 and failed == 0,
    )


def inspect_pg_local_embedding_state(
    store: Any,
    *,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    vector_scope: str | None = None,
    couchdb_store: Any | None = None,
    receipt_page_size: int = 64,
    receipt_max_rows: int = PG_RECEIPT_ROWS_MAX,
    receipt_max_seconds: float = 60,
) -> PgBackfillProgress:
    """Read-only inspection of PostgreSQL local embedding backfill and outbox state."""
    target_types = _pg_vector_scope_target_types(vector_scope)
    with store._scope(write=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM memory_cards
                 WHERE lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                   AND authorization_status = 'active'
                   AND currentness IN ('current', 'superseded')
                """
            )
            eligible_cards = _scalar_count(cur.fetchone())

            cur.execute(
                """
                SELECT COUNT(*)
                  FROM memory_cards mc
                  JOIN card_local_embeddings cle
                    ON cle.memory_id = mc.memory_id
                   AND cle.profile_id = %s
                   AND cle.source_content_hash = mc.content_hash
                   AND cle.state = 'ready'
                 WHERE mc.lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                   AND mc.authorization_status = 'active'
                   AND mc.currentness IN ('current', 'superseded')
                """,
                (profile_id,),
            )
            ready_cards = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(*) FROM session_memory_chunks")
            eligible_sessions = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(DISTINCT session_id_hash) FROM session_memory_chunks")
            eligible_session_receipts = _scalar_count(cur.fetchone())

            cur.execute(
                """
                SELECT COUNT(*)
                  FROM session_memory_chunks smc
                  JOIN session_local_embeddings sle
                    ON sle.chunk_id = smc.chunk_id
                   AND sle.profile_id = %s
                   AND sle.source_content_hash = smc.content_hash
                   AND sle.state = 'ready'
                """,
                (profile_id,),
            )
            ready_sessions = _scalar_count(cur.fetchone())

            ready_session_receipts = 0
            session_receipts_verified = False

            outbox_type_filter = " AND target_type = ANY(%s)" if target_types else ""
            outbox_params: tuple[Any, ...] = (
                (profile_id, list(target_types)) if target_types else (profile_id,)
            )
            cur.execute(
                f"""
                SELECT status AS status, COUNT(*) AS n
                  FROM local_embedding_outbox
                 WHERE profile_id = %s{outbox_type_filter}
                 GROUP BY status
                """,
                outbox_params,
            )
            outbox_counts = {str(_cell(row, "status", 0)): int(_cell(row, "n", 1)) for row in cur.fetchall() or ()}

            cur.execute(
                """
                SELECT COUNT(*)
                  FROM local_embedding_tombstones
                 WHERE profile_id = %s
                   AND deleted_at IS NOT NULL
                """,
                (profile_id,),
            )
            tombstones_count = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(*) FROM memory_cards WHERE embedding IS NOT NULL")
            legacy_cards = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(*) FROM session_memory_chunks WHERE embedding IS NOT NULL")
            legacy_sessions = _scalar_count(cur.fetchone())

    receipt_inspection = PgReceiptInspection(unchecked=eligible_session_receipts)
    if couchdb_store is not None:
        try:
            receipt_inspection = _inspect_pg_session_receipts(
                store,
                couchdb_store,
                profile_id=profile_id,
                eligible=eligible_session_receipts,
                page_size=receipt_page_size,
                max_rows=receipt_max_rows,
                max_seconds=receipt_max_seconds,
            )
        except Exception:
            receipt_inspection = PgReceiptInspection(
                failed=1,
                unchecked=eligible_session_receipts,
                verified=False,
            )
    ready_session_receipts = receipt_inspection.valid
    session_receipts_verified = receipt_inspection.verified
    missing_session_receipts = (
        receipt_inspection.missing + receipt_inspection.unchecked
        if couchdb_store is not None
        else eligible_session_receipts
    )

    return PgBackfillProgress(
        eligible_cards=eligible_cards,
        ready_cards=ready_cards,
        missing_cards=max(0, eligible_cards - ready_cards),
        eligible_sessions=eligible_sessions,
        ready_sessions=ready_sessions,
        missing_sessions=max(0, eligible_sessions - ready_sessions),
        eligible_session_receipts=eligible_session_receipts,
        ready_session_receipts=ready_session_receipts,
        missing_session_receipts=missing_session_receipts,
        session_receipts_verified=session_receipts_verified,
        session_receipts_checked=receipt_inspection.checked,
        conflicting_session_receipts=receipt_inspection.conflict,
        failed_session_receipts=receipt_inspection.failed,
        unchecked_session_receipts=receipt_inspection.unchecked,
        outbox_queued=outbox_counts.get("queued", 0),
        outbox_processing=outbox_counts.get("processing", 0),
        outbox_failed_retryable=outbox_counts.get("failed", 0),
        outbox_dead_letter=outbox_counts.get("dead_letter", 0),
        tombstones_count=tombstones_count,
        legacy_card_embeddings_preserved=legacy_cards,
        legacy_session_embeddings_preserved=legacy_sessions,
    )


def enqueue_missing_pg_local_embeddings(
    store: Any,
    *,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    limit_per_kind: int = 200,
    include_cards: bool = True,
    include_sessions: bool = True,
) -> tuple[int, int]:
    """Bounded enqueue of missing/stale cards and session_memory_chunks into local_embedding_outbox."""
    bounded_limit = max(1, min(int(limit_per_kind), 5000))
    enqueued_cards = 0
    enqueued_sessions = 0

    with store._scope(write=False) as conn:
        with conn.cursor() as cur:
            if include_cards:
                cur.execute(
                    """
                    SELECT mc.memory_id, mc.content_hash, mc.summary, mc.title, mc.typed_payload
                      FROM memory_cards mc
                      LEFT JOIN card_local_embeddings cle
                        ON cle.memory_id = mc.memory_id
                       AND cle.profile_id = %s
                       AND cle.source_content_hash = mc.content_hash
                       AND cle.state IN ('ready', 'failed')
                      LEFT JOIN local_embedding_tombstones t
                        ON t.target_type = 'memory_card'
                       AND t.target_id = mc.memory_id
                       AND t.profile_id = %s
                       AND t.deleted_at IS NOT NULL
                     WHERE mc.lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                       AND mc.authorization_status = 'active'
                       AND mc.currentness IN ('current', 'superseded')
                       AND cle.memory_id IS NULL
                       AND t.target_id IS NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM local_embedding_outbox o
                            WHERE o.target_type = 'memory_card'
                              AND o.target_id = mc.memory_id
                              AND o.profile_id = %s
                              AND o.source_content_hash = mc.content_hash
                              AND o.status IN ('queued', 'processing', 'failed', 'dead_letter')
                       )
                     ORDER BY mc.updated_at ASC, mc.memory_id ASC
                     LIMIT %s
                    """,
                    (profile_id, profile_id, profile_id, bounded_limit),
                )
                card_rows = cur.fetchall() or ()
            else:
                card_rows = ()

            if include_sessions:
                cur.execute(
                    """
                    SELECT smc.chunk_id, smc.content_hash, smc.content_markdown
                      FROM session_memory_chunks smc
                      LEFT JOIN session_local_embeddings sle
                        ON sle.chunk_id = smc.chunk_id
                       AND sle.profile_id = %s
                       AND sle.source_content_hash = smc.content_hash
                       AND sle.state IN ('ready', 'failed')
                      LEFT JOIN local_embedding_tombstones t
                        ON t.target_type = 'session_chunk'
                       AND t.target_id = smc.chunk_id
                       AND t.profile_id = %s
                       AND t.deleted_at IS NOT NULL
                     WHERE sle.chunk_id IS NULL
                       AND t.target_id IS NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM local_embedding_outbox o
                            WHERE o.target_type = 'session_chunk'
                              AND o.target_id = smc.chunk_id
                              AND o.profile_id = %s
                              AND o.source_content_hash = smc.content_hash
                              AND o.status IN ('queued', 'processing', 'failed', 'dead_letter')
                       )
                     ORDER BY smc.updated_at ASC, smc.chunk_id ASC
                     LIMIT %s
                    """,
                    (profile_id, profile_id, profile_id, bounded_limit),
                )
                session_rows = cur.fetchall() or ()
            else:
                session_rows = ()

    for card_row in card_rows:
        memory_id = _cell(card_row, "memory_id", 0)
        content_hash = _cell(card_row, "content_hash", 1)
        summary = _cell(card_row, "summary", 2)
        title = _cell(card_row, "title", 3)
        typed_payload_raw = _cell(card_row, "typed_payload", 4)
        typed_payload = (
            typed_payload_raw
            if isinstance(typed_payload_raw, dict)
            else (
                json.loads(typed_payload_raw)
                if isinstance(typed_payload_raw, str) and typed_payload_raw.strip()
                else {}
            )
        )
        try:
            payload_text = card_input_v1(
                summary=str(summary or ""),
                title=str(title or ""),
                typed_payload=typed_payload,
            )
            store.enqueue_local_outbox(
                target_type="memory_card",
                target_id=str(memory_id),
                source_content_hash=str(content_hash),
                payload_text=payload_text,
                profile_id=profile_id,
            )
            enqueued_cards += 1
        except ValueError:
            # Parent deleted or modified concurrently; skip safely
            continue

    for session_row in session_rows:
        chunk_id = _cell(session_row, "chunk_id", 0)
        content_hash = _cell(session_row, "content_hash", 1)
        content_markdown = _cell(session_row, "content_markdown", 2)
        try:
            payload_text = session_input_v1(str(content_markdown or ""))
            store.enqueue_local_outbox(
                target_type="session_chunk",
                target_id=str(chunk_id),
                source_content_hash=str(content_hash),
                payload_text=payload_text,
                profile_id=profile_id,
            )
            enqueued_sessions += 1
        except ValueError:
            continue

    return enqueued_cards, enqueued_sessions


def _pg_vector_scope_target_types(vector_scope: str | None) -> tuple[str, ...] | None:
    if vector_scope is None:
        return None
    scope_types = {
        "cards": ("memory_card",),
        "sessions": ("session_chunk",),
        "all": ("memory_card", "session_chunk"),
    }
    normalized = str(vector_scope).strip().lower()
    if normalized not in scope_types:
        raise ValueError("invalid pg vector scope")
    return scope_types[normalized]


def _resolve_embedding_fn(embedder: Any) -> Callable[[str], list[float]]:
    if callable(embedder) and not hasattr(embedder, "embed") and not hasattr(embedder, "embed_texts"):
        return embedder
    if hasattr(embedder, "embed"):
        return embedder.embed
    if hasattr(embedder, "embed_query"):
        return embedder.embed_query
    if hasattr(embedder, "embed_texts"):
        return lambda text: list(embedder.embed_texts([text])[0].vector)
    raise TypeError(f"Unsupported embedder for LocalEmbeddingOutboxWorker: {type(embedder)!r}")


def backfill_pg_local_embeddings(
    store: Any,
    embedder: Any,
    *,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    batch_size: int = 32,
    max_batches: int = 10,
    enqueue_limit_per_kind: int = 200,
    lease_seconds: int = 30,
    max_retries: int = 5,
    worker_id: str | None = None,
    couchdb_store: Any | None = None,
    vector_scope: str | None = None,
    max_seconds: float = 300,
    receipt_checkpoint_path: Path | str | None = None,
    receipt_page_size: int = 64,
    receipt_max_pages: int = 10,
    receipt_max_rows: int = 500,
    receipt_max_seconds: float = 60,
    receipt_run_id: str | None = None,
) -> PgBackfillProgress:
    """Bounded, idempotent, resumable PostgreSQL backfill for cards and session chunks."""
    from ..postgres_store.outbox_worker import LocalEmbeddingOutboxWorker

    bounded_batch = max(1, min(int(batch_size), 64))
    bounded_max_batches = max(1, int(max_batches))
    target_types = _pg_vector_scope_target_types(vector_scope)
    include_cards = target_types is None or "memory_card" in target_types
    include_sessions = target_types is None or "session_chunk" in target_types
    run_started = time.monotonic()
    run_deadline = run_started + max(1.0, min(float(max_seconds), 900.0))
    enq_cards, enq_sessions = enqueue_missing_pg_local_embeddings(
        store,
        profile_id=profile_id,
        limit_per_kind=enqueue_limit_per_kind,
        include_cards=include_cards,
        include_sessions=include_sessions,
    )
    worker = LocalEmbeddingOutboxWorker(
        store=store,
        worker_id=worker_id or f"m3-pg-backfill-{uuid.uuid4().hex[:8]}",
        batch_size=bounded_batch,
        lease_seconds=lease_seconds,
        embedding_fn=_resolve_embedding_fn(embedder),
        max_retries=max_retries,
        embedding_profile=profile_id,
        target_types=target_types,
    )
    batches_executed = 0
    total_claimed = 0
    total_completed = 0
    total_stale = 0
    total_failed = 0

    for _ in range(bounded_max_batches):
        if time.monotonic() >= run_deadline:
            break
        claimed = worker.run_once(batch_size=bounded_batch)
        if claimed == 0:
            break
        batches_executed += 1
        total_claimed += claimed
        total_completed += worker.last_batch_completed
        total_stale += worker.last_batch_stale
        total_failed += worker.last_batch_failed

    receipt_before = (
        inspect_pg_local_embedding_state(
            store,
            profile_id=profile_id,
            couchdb_store=couchdb_store,
            receipt_page_size=receipt_page_size,
            receipt_max_rows=receipt_max_rows,
            receipt_max_seconds=min(receipt_max_seconds, max(1.0, max_seconds)),
        )
        if couchdb_store is not None
        else None
    )
    receipt_progress = (
        process_pg_local_embedding_receipts(
            store,
            couchdb_store,
            profile_id=profile_id,
            checkpoint_path=receipt_checkpoint_path,
            page_size=receipt_page_size,
            max_pages=receipt_max_pages,
            max_rows=receipt_max_rows,
            max_seconds=min(receipt_max_seconds, max(1.0, max_seconds)),
            run_id=receipt_run_id,
        )
        if couchdb_store is not None
        else PgReceiptProgress()
    )
    snapshot = inspect_pg_local_embedding_state(
        store,
        profile_id=profile_id,
        vector_scope=vector_scope,
        couchdb_store=couchdb_store,
        receipt_page_size=receipt_page_size,
        receipt_max_rows=receipt_max_rows,
        receipt_max_seconds=min(receipt_max_seconds, max(1.0, max_seconds)),
    )
    return PgBackfillProgress(
        eligible_cards=snapshot.eligible_cards,
        ready_cards=snapshot.ready_cards,
        missing_cards=snapshot.missing_cards,
        eligible_sessions=snapshot.eligible_sessions,
        ready_sessions=snapshot.ready_sessions,
        missing_sessions=snapshot.missing_sessions,
        eligible_session_receipts=snapshot.eligible_session_receipts,
        ready_session_receipts=snapshot.ready_session_receipts,
        missing_session_receipts=snapshot.missing_session_receipts,
        session_receipts_verified=snapshot.session_receipts_verified,
        session_receipts_checked=snapshot.session_receipts_checked,
        conflicting_session_receipts=snapshot.conflicting_session_receipts,
        failed_session_receipts=snapshot.failed_session_receipts,
        unchecked_session_receipts=snapshot.unchecked_session_receipts,
        session_receipts_verified_before=bool(receipt_before and receipt_before.session_receipts_verified),
        valid_session_receipts_before=receipt_before.ready_session_receipts if receipt_before else 0,
        missing_session_receipts_before=receipt_before.missing_session_receipts if receipt_before else 0,
        conflicting_session_receipts_before=receipt_before.conflicting_session_receipts if receipt_before else 0,
        failed_session_receipts_before=receipt_before.failed_session_receipts if receipt_before else 0,
        receipt_rows_processed=receipt_progress.rows_processed,
        receipt_pages_processed=receipt_progress.pages_processed,
        receipt_created=receipt_progress.created,
        receipt_already_valid=receipt_progress.already_valid,
        receipt_conflict=receipt_progress.conflict,
        receipt_failed=receipt_progress.failed,
        receipt_mutation_unknown=receipt_progress.mutation_unknown,
        receipt_stale_skipped=receipt_progress.stale_skipped,
        enqueued_cards=enq_cards,
        enqueued_sessions=enq_sessions,
        batches_executed=batches_executed,
        claimed=total_claimed,
        completed=total_completed,
        stale_fenced=total_stale,
        retried=total_failed,
        quarantined=snapshot.outbox_dead_letter,
        outbox_queued=snapshot.outbox_queued,
        outbox_processing=snapshot.outbox_processing,
        outbox_failed_retryable=snapshot.outbox_failed_retryable,
        outbox_dead_letter=snapshot.outbox_dead_letter,
        tombstones_count=snapshot.tombstones_count,
        legacy_card_embeddings_preserved=snapshot.legacy_card_embeddings_preserved,
        legacy_session_embeddings_preserved=snapshot.legacy_session_embeddings_preserved,
    )


def inspect_neo4j_local_embedding_state(
    session: Any,
    *,
    group_ids: list[str] | None = None,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    page_size: int = GRAPH_READBACK_PAGE_SIZE,
) -> Neo4jBackfillProgress:
    """Read back graph targets in bounded pages and verify exact current identities."""
    bounded_page_size = int(page_size)
    if not 1 <= bounded_page_size <= GRAPH_READBACK_PAGE_SIZE:
        raise ValueError("graph readback page size exceeds the hard bound")

    node_group_filter = "AND n.group_id IN $group_ids" if group_ids is not None else ""
    edge_group_filter = "AND e.group_id IN $group_ids" if group_ids is not None else ""
    tombstone_group_filter = "WHERE t.group_id IN $group_ids" if group_ids is not None else ""
    fence_group_filter = "WHERE f.group_id IN $group_ids" if group_ids is not None else ""
    base_params: dict[str, Any] = {"group_ids": list(group_ids or ())}
    discovered_groups: set[str] = set()
    counters = {
        "eligible_entities": 0,
        "ready_entities": 0,
        "eligible_edges": 0,
        "ready_edges": 0,
        "legacy_entities": 0,
        "legacy_edges": 0,
        "input_hash_mismatches": 0,
        "profile_mismatches": 0,
        "invalid_vectors": 0,
        "dirty_targets": 0,
        "tombstoned_targets": 0,
        "tombstoned_embeddings": 0,
    }

    def scan_targets(*, target_kind: str) -> bool:
        after_uuid: str | None = None
        is_entity = target_kind == "entity"
        if is_entity:
            match = "MATCH (n:Entity)"
            group_filter = node_group_filter
            target_expression = "n"
            input_text_expression = "n.name"
            vector_expression = "n.name_embedding_model2vec_v1"
            profile_expression = "n.name_embedding_model2vec_v1_profile_id"
            input_hash_expression = "n.name_embedding_model2vec_v1_input_hash"
            legacy_expression = "n.name_embedding"
        else:
            match = "MATCH (:Entity)-[e:RELATES_TO]->(:Entity)"
            group_filter = edge_group_filter
            target_expression = "e"
            input_text_expression = "e.fact"
            vector_expression = "e.fact_embedding_model2vec_v1"
            profile_expression = "e.fact_embedding_model2vec_v1_profile_id"
            input_hash_expression = "e.fact_embedding_model2vec_v1_input_hash"
            legacy_expression = "e.fact_embedding"

        while True:
            result = session.run(
                f"""
                {match}
                WHERE {target_expression}.uuid IS NOT NULL
                  {group_filter}
                  AND ($after_uuid IS NULL OR {target_expression}.uuid > $after_uuid)
                OPTIONAL MATCH (f:EmbeddingGroupFence {{group_id: {target_expression}.group_id}})
                RETURN {target_expression}.uuid AS target_id,
                       {target_expression}.group_id AS group_id,
                       {input_text_expression} AS source_text,
                       {vector_expression} AS vector,
                       {profile_expression} AS profile_id,
                       {input_hash_expression} AS input_hash,
                       coalesce({target_expression}.embedding_dirty, false) AS dirty,
                       {legacy_expression} AS legacy_vector,
                       EXISTS {{ MATCH (d:EmbeddingDeletion {{target_type: '{target_kind}', target_uuid: {target_expression}.uuid}}) }} AS tombstoned,
                       f.status AS fence_status,
                       (f.lease_owner IS NOT NULL AND f.lease_until > datetime()) AS fence_busy
                ORDER BY {target_expression}.uuid ASC
                LIMIT $limit
                """,
                **base_params,
                after_uuid=after_uuid,
                limit=bounded_page_size,
            )
            rows = [dict(row) for row in (result.data() or ())]
            if len(rows) > bounded_page_size:
                return False
            if not rows:
                return True

            for row in rows:
                target_id = str(row.get("target_id") or "")
                if not target_id or (after_uuid is not None and target_id <= after_uuid):
                    return False
                after_uuid = target_id
                group_id = str(row.get("group_id") or "")
                if group_id:
                    discovered_groups.add(group_id)

                vector = row.get("vector")
                vector_present = vector is not None
                legacy_key = "legacy_entities" if is_entity else "legacy_edges"
                if row.get("legacy_vector") is not None:
                    counters[legacy_key] += 1

                if bool(row.get("tombstoned")):
                    counters["tombstoned_targets"] += 1
                    if vector_present:
                        counters["tombstoned_embeddings"] += 1
                    continue

                eligible_key = "eligible_entities" if is_entity else "eligible_edges"
                ready_key = "ready_entities" if is_entity else "ready_edges"
                counters[eligible_key] += 1

                source_text = row.get("source_text")
                expected_hash = (
                    compute_graph_node_input_hash(str(source_text))
                    if is_entity
                    else compute_graph_edge_input_hash(str(source_text))
                )
                if not isinstance(source_text, str) or row.get("input_hash") != expected_hash:
                    counters["input_hash_mismatches"] += 1
                if row.get("profile_id") != profile_id:
                    counters["profile_mismatches"] += 1
                vector_is_valid = (
                    isinstance(vector, (list, tuple))
                    and len(vector) == LOCAL_EMBEDDING_DIM
                    and all(
                        not isinstance(value, bool)
                        and isinstance(value, (int, float))
                        and math.isfinite(float(value))
                        for value in vector
                    )
                )
                if not vector_is_valid:
                    counters["invalid_vectors"] += 1
                if bool(row.get("dirty")):
                    counters["dirty_targets"] += 1
                if (
                    row.get("input_hash") == expected_hash
                    and row.get("profile_id") == profile_id
                    and vector_is_valid
                    and not bool(row.get("dirty"))
                ):
                    counters[ready_key] += 1

            if len(rows) < bounded_page_size:
                return True

    entities_scan_complete = scan_targets(target_kind="entity")
    edges_scan_complete = scan_targets(target_kind="edge")

    tomb_rec = session.run(
        f"""
        MATCH (t:EmbeddingDeletion)
        {tombstone_group_filter}
        RETURN count(t) AS tombstones
        """,
        **base_params,
    ).single()
    tombstones_count = int(dict(tomb_rec).get("tombstones") or 0) if tomb_rec is not None else 0

    fence_rec = session.run(
        f"""
        MATCH (f:EmbeddingGroupFence)
        {fence_group_filter}
        RETURN count(CASE WHEN f.status = 'quarantined' THEN 1 END) AS quarantined_groups,
               count(CASE WHEN f.status <> 'quarantined'
                               AND f.lease_owner IS NOT NULL
                               AND f.lease_until > datetime()
                          THEN 1 END) AS busy_groups
        """,
        **base_params,
    ).single()
    quarantined_groups = (
        int(dict(fence_rec).get("quarantined_groups") or 0) if fence_rec is not None else 0
    )
    busy_groups = int(dict(fence_rec).get("busy_groups") or 0) if fence_rec is not None else 0
    readback_complete = bool(
        entities_scan_complete
        and edges_scan_complete
        and tomb_rec is not None
        and fence_rec is not None
    )
    eligible_entities = counters["eligible_entities"]
    ready_entities = counters["ready_entities"]
    eligible_edges = counters["eligible_edges"]
    ready_edges = counters["ready_edges"]
    readback_verified = bool(
        readback_complete
        and ready_entities == eligible_entities
        and ready_edges == eligible_edges
        and counters["input_hash_mismatches"] == 0
        and counters["profile_mismatches"] == 0
        and counters["invalid_vectors"] == 0
        and counters["dirty_targets"] == 0
        and counters["tombstoned_embeddings"] == 0
        and quarantined_groups == 0
        and busy_groups == 0
    )

    return Neo4jBackfillProgress(
        group_ids_scanned=tuple(sorted(discovered_groups)),
        eligible_entities=eligible_entities,
        ready_entities=ready_entities,
        missing_or_dirty_entities=max(0, eligible_entities - ready_entities),
        eligible_edges=eligible_edges,
        ready_edges=ready_edges,
        missing_or_dirty_edges=max(0, eligible_edges - ready_edges),
        groups_fenced_busy=busy_groups,
        groups_quarantined=quarantined_groups,
        tombstones_count=tombstones_count,
        legacy_entity_embeddings_preserved=counters["legacy_entities"],
        legacy_edge_embeddings_preserved=counters["legacy_edges"],
        readback_complete=readback_complete,
        readback_verified=readback_verified,
        readback_input_hash_mismatches=counters["input_hash_mismatches"],
        readback_profile_mismatches=counters["profile_mismatches"],
        readback_invalid_vectors=counters["invalid_vectors"],
        readback_dirty_targets=counters["dirty_targets"],
        readback_tombstoned_targets=counters["tombstoned_targets"],
        readback_tombstoned_embeddings=counters["tombstoned_embeddings"],
    )


def _verify_neo4j_embedding_batch(
    session: Any,
    *,
    group_id: str,
    entity_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    profile_id: str,
) -> bool:
    """Verify every batch target before its durable operation-step receipt is written."""
    for target_kind, candidates in (("entity", entity_rows), ("edge", edge_rows)):
        if not candidates:
            continue
        if target_kind == "entity":
            match = "OPTIONAL MATCH (n:Entity {uuid: expected.uuid, group_id: $group_id})"
            source_expression = "n.name"
            vector_expression = "n.name_embedding_model2vec_v1"
            profile_expression = "n.name_embedding_model2vec_v1_profile_id"
            input_hash_expression = "n.name_embedding_model2vec_v1_input_hash"
        else:
            match = "OPTIONAL MATCH ()-[n:RELATES_TO {uuid: expected.uuid, group_id: $group_id}]->()"
            source_expression = "n.fact"
            vector_expression = "n.fact_embedding_model2vec_v1"
            profile_expression = "n.fact_embedding_model2vec_v1_profile_id"
            input_hash_expression = "n.fact_embedding_model2vec_v1_input_hash"

        expected_targets = [
            {
                "uuid": str(row.get("uuid") or ""),
                "input_hash": (
                    compute_graph_node_input_hash(str(row.get("name") or ""))
                    if target_kind == "entity"
                    else compute_graph_edge_input_hash(str(row.get("fact") or ""))
                ),
            }
            for row in candidates
        ]
        rows = session.run(
            f"""
            UNWIND $targets AS expected
            {match}
            OPTIONAL MATCH (d:EmbeddingDeletion {{
                target_type: '{target_kind}', target_uuid: expected.uuid
            }})
            RETURN expected.uuid AS expected_uuid,
                   n.uuid AS target_id,
                   n.group_id AS group_id,
                   {source_expression} AS source_text,
                   {vector_expression} AS vector,
                   {profile_expression} AS profile_id,
                   {input_hash_expression} AS input_hash,
                   coalesce(n.embedding_dirty, false) AS dirty,
                   d.target_uuid IS NOT NULL AS tombstoned
            """,
            group_id=group_id,
            targets=expected_targets,
        ).data()
        if len(rows or ()) != len(expected_targets):
            return False

        expected_by_id = {target["uuid"]: target["input_hash"] for target in expected_targets}
        observed_ids: set[str] = set()
        for raw_row in rows or ():
            row = dict(raw_row)
            target_id = str(row.get("target_id") or "")
            expected_id = str(row.get("expected_uuid") or "")
            source_text = row.get("source_text")
            expected_hash = expected_by_id.get(expected_id)
            actual_hash = (
                compute_graph_node_input_hash(source_text)
                if target_kind == "entity" and isinstance(source_text, str)
                else compute_graph_edge_input_hash(source_text)
                if isinstance(source_text, str)
                else ""
            )
            vector = row.get("vector")
            vector_valid = (
                isinstance(vector, (list, tuple))
                and len(vector) == LOCAL_EMBEDDING_DIM
                and all(
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(float(value))
                    for value in vector
                )
            )
            if (
                not target_id
                or target_id != expected_id
                or target_id in observed_ids
                or row.get("group_id") != group_id
                or actual_hash != expected_hash
                or row.get("input_hash") != expected_hash
                or row.get("profile_id") != profile_id
                or not vector_valid
                or bool(row.get("dirty"))
                or bool(row.get("tombstoned"))
            ):
                return False
            observed_ids.add(target_id)
        if len(observed_ids) != len(expected_targets):
            return False
    return True


def backfill_neo4j_local_embeddings(
    session: Any,
    embedder: Any,
    *,
    group_ids: list[str] | None = None,
    batch_size: int = 32,
    max_batches_per_group: int = 10,
    lease_seconds: int = 120,
    worker_id: str | None = None,
    journal_coordinator: GraphOperationJournalCoordinator | None = None,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    max_seconds: float = 300,
    ensure_schema: bool = True,
) -> Neo4jBackfillProgress:
    """Run bounded, batch-journaled Neo4j writes with fenced readback."""
    bounded_batch = max(1, min(int(batch_size), 64))
    bounded_max_batches = max(1, int(max_batches_per_group))
    time_limit = float(max_seconds)
    if not math.isfinite(time_limit) or not 0 < time_limit <= 600:
        raise ValueError("max_seconds exceeds the hard bound")
    if int(lease_seconds) < 1 or int(lease_seconds) > 300:
        raise ValueError("lease_seconds exceeds the hard bound")
    started = time.monotonic()
    resolved_worker_id = worker_id or f"m3-neo4j-backfill-{uuid.uuid4().hex[:8]}"

    embed_base_url = getattr(embedder, "base_url", None) or getattr(
        getattr(embedder, "_config", None), "base_url", None
    )
    if embed_base_url:
        validate_local_embedding_write_or_query_policy(
            profile_id=profile_id,
            model=LOCAL_EMBEDDING_MODEL_ALIAS,
            base_url=str(embed_base_url),
            dimension=LOCAL_EMBEDDING_DIM,
        )

    if ensure_schema:
        ensure_neo4j_safety_schema(session)

    if group_ids is None:
        grp_rec = session.run(
            """
            MATCH (n:Entity)
            WHERE n.group_id IS NOT NULL AND n.group_id <> ''
            RETURN collect(DISTINCT n.group_id) AS groups
            """
        ).single()
        target_groups = sorted(
            str(g)
            for g in (dict(grp_rec).get("groups") if grp_rec is not None else () or ())
            if g
        )
    else:
        target_groups = sorted({str(g).strip() for g in group_ids if str(g).strip()})
    if len(target_groups) > 4096:
        raise ValueError("target group count exceeds the hard bound")

    entities_embedded = 0
    entities_fenced_stale = 0
    edges_embedded = 0
    edges_fenced_stale = 0
    groups_fenced_busy = 0
    groups_quarantined = 0
    groups_conflicted = 0
    batches_attempted = 0
    model_calls = 0
    stop_reason = "completed"

    def read_batch(group_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ent_rows = session.run(
            """
            MATCH (n:Entity {group_id: $group_id})
            OPTIONAL MATCH (d:EmbeddingDeletion {target_type: 'entity', target_uuid: n.uuid})
            WITH n, d
            WHERE d IS NULL AND (
                n.name_embedding_model2vec_v1 IS NULL
                OR coalesce(n.embedding_dirty, false) = true
                OR n.name_embedding_model2vec_v1_input_hash IS NULL
            )
            RETURN n.uuid AS uuid, n.name AS name,
                   n.name_embedding_model2vec_v1_input_hash AS current_hash
            ORDER BY n.uuid ASC LIMIT $limit
            """,
            group_id=group_id,
            limit=bounded_batch,
        ).data()
        edge_rows = session.run(
            """
            MATCH (:Entity)-[e:RELATES_TO {group_id: $group_id}]->(:Entity)
            OPTIONAL MATCH (d:EmbeddingDeletion {target_type: 'edge', target_uuid: e.uuid})
            WITH e, d
            WHERE d IS NULL AND (
                e.fact_embedding_model2vec_v1 IS NULL
                OR coalesce(e.embedding_dirty, false) = true
                OR e.fact_embedding_model2vec_v1_input_hash IS NULL
            )
            RETURN e.uuid AS uuid, e.fact AS fact,
                   e.fact_embedding_model2vec_v1_input_hash AS current_hash
            ORDER BY e.uuid ASC LIMIT $limit
            """,
            group_id=group_id,
            limit=bounded_batch,
        ).data()
        return [dict(row) for row in (ent_rows or ())], [dict(row) for row in (edge_rows or ())]

    def batch_identity(
        group_id: str,
        entity_rows: list[dict[str, Any]],
        edge_rows: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        entity_parts = sorted(
            (
                hashlib.sha256(str(row.get("uuid") or "").encode("utf-8")).hexdigest(),
                compute_graph_node_input_hash(str(row.get("name") or "")),
            )
            for row in entity_rows
        )
        edge_parts = sorted(
            (
                hashlib.sha256(str(row.get("uuid") or "").encode("utf-8")).hexdigest(),
                compute_graph_edge_input_hash(str(row.get("fact") or "")),
            )
            for row in edge_rows
        )
        identity = {
            "profile_id": profile_id,
            "group_fingerprint": hashlib.sha256(group_id.encode("utf-8")).hexdigest(),
            "entities": entity_parts,
            "edges": edge_parts,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        effect = {
            "profile_id": profile_id,
            "group_fingerprint": identity["group_fingerprint"],
            "batch_fingerprint": "sha256:" + digest,
            "entity_count": len(entity_rows),
            "edge_count": len(edge_rows),
        }
        return "model2vec-batch:" + digest, effect

    for group_id in target_groups:
        for _batch_idx in range(bounded_max_batches):
            if time.monotonic() - started >= time_limit:
                stop_reason = "time_limit"
                break
            entity_candidates, edge_candidates = read_batch(group_id)
            if not entity_candidates and not edge_candidates:
                break
            operation_id, effect_payload = batch_identity(
                group_id, entity_candidates, edge_candidates
            )
            try:
                if journal_coordinator is not None:
                    lease = journal_coordinator.begin_operation(
                        session,
                        operation_id=operation_id,
                        group_id=group_id,
                        operation_type="graph_local_embedding_backfill",
                        lease_owner=resolved_worker_id,
                        lease_seconds=lease_seconds,
                        fence_status="backfill_in_progress",
                        payload=effect_payload,
                    )
                else:
                    lease = acquire_group_fence_lease(
                        session,
                        group_id,
                        lease_owner=resolved_worker_id,
                        operation_id=operation_id,
                        status="backfill_in_progress",
                        lease_seconds=lease_seconds,
                        bump_epoch=False,
                    )
            except GroupFenceBusyError:
                groups_fenced_busy += 1
                break
            except GroupQuarantinedError:
                groups_quarantined += 1
                break
            except GraphOperationConflictQuarantinedError:
                groups_conflicted += 1
                groups_quarantined += 1
                stop_reason = "operation_conflict"
                break

            try:
                locked_entities, locked_edges = read_batch(group_id)
                locked_operation_id, locked_effect = batch_identity(
                    group_id, locked_entities, locked_edges
                )
                if locked_operation_id != operation_id or locked_effect != effect_payload:
                    stop_reason = "source_changed_before_batch"
                    break

                batches_attempted += 1

                def apply_batch() -> dict[str, int]:
                    nonlocal entities_embedded, entities_fenced_stale
                    nonlocal edges_embedded, edges_fenced_stale, model_calls
                    written_entities = 0
                    written_edges = 0
                    stale_writes = 0
                    if entity_candidates:
                        inputs = [
                            format_graph_node_embedding_input(str(row.get("name") or ""))
                            for row in entity_candidates
                        ]
                        hashes = [
                            compute_graph_node_input_hash(str(row.get("name") or ""))
                            for row in entity_candidates
                        ]
                        model_calls += 1
                        vectors = _invoke_embedder_batch(embedder, inputs)
                        for candidate, expected_hash, vector in zip(
                            entity_candidates, hashes, vectors, strict=True
                        ):
                            result = cas_write_entity_local_embedding(
                                session,
                                entity_uuid=str(candidate["uuid"]),
                                group_id=group_id,
                                expected_name=str(candidate.get("name") or ""),
                                expected_input_hash=expected_hash,
                                expected_epoch=lease.epoch,
                                vector=vector,
                                lease_owner=resolved_worker_id,
                            )
                            if result.applied:
                                entities_embedded += 1
                                written_entities += 1
                            else:
                                entities_fenced_stale += 1
                                stale_writes += 1
                    if edge_candidates:
                        inputs = [
                            format_graph_edge_embedding_input(str(row.get("fact") or ""))
                            for row in edge_candidates
                        ]
                        hashes = [
                            compute_graph_edge_input_hash(str(row.get("fact") or ""))
                            for row in edge_candidates
                        ]
                        model_calls += 1
                        vectors = _invoke_embedder_batch(embedder, inputs)
                        for candidate, expected_hash, vector in zip(
                            edge_candidates, hashes, vectors, strict=True
                        ):
                            result = cas_write_edge_local_embedding(
                                session,
                                edge_uuid=str(candidate["uuid"]),
                                group_id=group_id,
                                expected_fact=str(candidate.get("fact") or ""),
                                expected_input_hash=expected_hash,
                                expected_epoch=lease.epoch,
                                vector=vector,
                                lease_owner=resolved_worker_id,
                            )
                            if result.applied:
                                edges_embedded += 1
                                written_edges += 1
                            else:
                                edges_fenced_stale += 1
                                stale_writes += 1
                    if stale_writes:
                        raise _Neo4jStaleBatchWrite("stale_embedding_batch_write")
                    if not _verify_neo4j_embedding_batch(
                        session,
                        group_id=group_id,
                        entity_rows=entity_candidates,
                        edge_rows=edge_candidates,
                        profile_id=profile_id,
                    ):
                        raise _Neo4jBatchReadbackIncomplete("embedding_batch_readback_incomplete")
                    return {"entities_written": written_entities, "edges_written": written_edges}

                if journal_coordinator is not None:
                    outcome, _result = journal_coordinator.run_step(
                        session,
                        operation_id=operation_id,
                        group_id=group_id,
                        step_id="embeddings_committed",
                        effect_payload=effect_payload,
                        action=apply_batch,
                    )
                else:
                    outcome = "executed"
                    apply_batch()
                if outcome == "noop":
                    if not _verify_neo4j_embedding_batch(
                        session,
                        group_id=group_id,
                        entity_rows=entity_candidates,
                        edge_rows=edge_candidates,
                        profile_id=profile_id,
                    ):
                        from ..llm_brain_core.local_graph_safety import quarantine_group_fence

                        quarantine_group_fence(
                            session,
                            group_id,
                            operation_id=operation_id,
                            reason="journal_noop_readback_incomplete",
                        )
                        groups_quarantined += 1
                        stop_reason = "journal_readback_incomplete"
                        break
                    remaining_entities, remaining_edges = read_batch(group_id)
                    if remaining_entities or remaining_edges:
                        from ..llm_brain_core.local_graph_safety import quarantine_group_fence

                        quarantine_group_fence(
                            session,
                            group_id,
                            operation_id=operation_id,
                            reason="journal_noop_readback_incomplete",
                        )
                        groups_quarantined += 1
                        stop_reason = "journal_readback_incomplete"
                        break
            except GraphOperationConflictQuarantinedError:
                groups_conflicted += 1
                groups_quarantined += 1
                stop_reason = "operation_conflict"
                break
            except _Neo4jStaleBatchWrite:
                stop_reason = "stale_fenced"
                break
            except _Neo4jBatchReadbackIncomplete:
                stop_reason = "batch_readback_incomplete"
                break
            except GroupQuarantinedError:
                groups_quarantined += 1
                stop_reason = "group_quarantined"
                break
            except Exception:
                stop_reason = "batch_failed"
                break
            finally:
                try:
                    if journal_coordinator is not None:
                        journal_coordinator.complete_operation(session, lease=lease)
                    else:
                        release_group_fence_lease(session, lease)
                except Exception:
                    stop_reason = "fence_release_failed"
            if stop_reason != "completed":
                break
        if stop_reason != "completed":
            break
        if time.monotonic() - started >= time_limit:
            stop_reason = "time_limit"
            break

    snapshot = inspect_neo4j_local_embedding_state(
        session,
        group_ids=target_groups,
        profile_id=profile_id,
    )
    return Neo4jBackfillProgress(
        group_ids_scanned=snapshot.group_ids_scanned,
        eligible_entities=snapshot.eligible_entities,
        ready_entities=snapshot.ready_entities,
        missing_or_dirty_entities=snapshot.missing_or_dirty_entities,
        eligible_edges=snapshot.eligible_edges,
        ready_edges=snapshot.ready_edges,
        missing_or_dirty_edges=snapshot.missing_or_dirty_edges,
        entities_embedded=entities_embedded,
        entities_fenced_stale=entities_fenced_stale,
        edges_embedded=edges_embedded,
        edges_fenced_stale=edges_fenced_stale,
        groups_fenced_busy=max(groups_fenced_busy, snapshot.groups_fenced_busy),
        groups_quarantined=max(groups_quarantined, snapshot.groups_quarantined),
        groups_conflicted=groups_conflicted,
        batches_attempted=batches_attempted,
        model_calls=model_calls,
        stop_reason=(
            stop_reason
            if stop_reason != "completed"
            else "group_busy" if groups_fenced_busy else "group_quarantined" if groups_quarantined else "completed"
        ),
        tombstones_count=snapshot.tombstones_count,
        legacy_entity_embeddings_preserved=snapshot.legacy_entity_embeddings_preserved,
        legacy_edge_embeddings_preserved=snapshot.legacy_edge_embeddings_preserved,
        readback_complete=snapshot.readback_complete,
        readback_verified=snapshot.readback_verified,
        readback_input_hash_mismatches=snapshot.readback_input_hash_mismatches,
        readback_profile_mismatches=snapshot.readback_profile_mismatches,
        readback_invalid_vectors=snapshot.readback_invalid_vectors,
        readback_dirty_targets=snapshot.readback_dirty_targets,
        readback_tombstoned_targets=snapshot.readback_tombstoned_targets,
        readback_tombstoned_embeddings=snapshot.readback_tombstoned_embeddings,
    )


def _invoke_embedder_batch(embedder: Any, texts: list[str]) -> list[list[float]]:
    if hasattr(embedder, "embed_batch"):
        return [list(vec) for vec in embedder.embed_batch(texts)]
    if hasattr(embedder, "embed_texts"):
        result = embedder.embed_texts(texts)
        return [
            list(item.vector) if hasattr(item, "vector") else list(item)
            for item in result
        ]
    if hasattr(embedder, "embed_documents"):
        result = embedder.embed_documents(texts)
        return [
            list(item.vector) if hasattr(item, "vector") else list(item)
            for item in result
        ]
    if hasattr(embedder, "embed_many"):
        raw = embedder.embed_many(texts)
        if any(vec is None for vec in raw):
            raise RuntimeError("embed_many returned None for one or more texts")
        return [list(vec) for vec in raw if vec is not None]
    if hasattr(embedder, "embed"):
        return [list(embedder.embed(t)) for t in texts]
    if callable(embedder):
        return [list(embedder(t)) for t in texts]
    raise TypeError(f"Unsupported embedder type for backfill: {type(embedder)!r}")


def evaluate_cutover_readiness(
    *,
    pg_progress: PgBackfillProgress | None = None,
    neo4j_progress: Neo4jBackfillProgress | None = None,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    direct_migrated_reader: bool | None = None,
) -> LocalEmbeddingCutoverReport:
    """Fail-closed cutover readiness check across PostgreSQL and Neo4j."""
    is_direct = (
        direct_migrated_reader
        if direct_migrated_reader is not None
        else (os.getenv("PG_RECALL_DIRECT_MIGRATED", "").strip().casefold() == "true")
    )
    blocking: list[str] = []
    if pg_progress is None and neo4j_progress is None:
        blocking.append("no_backend_inspected")

    if pg_progress is not None:
        if pg_progress.missing_cards > 0:
            blocking.append(f"pg_missing_cards={pg_progress.missing_cards}")
        if pg_progress.missing_sessions > 0:
            blocking.append(f"pg_missing_sessions={pg_progress.missing_sessions}")
        if not is_direct:
            if not pg_progress.session_receipts_verified:
                blocking.append("pg_session_receipts_unverified_for_couchdb_authority")
            elif pg_progress.missing_session_receipts > 0:
                blocking.append(f"pg_missing_session_receipts={pg_progress.missing_session_receipts}")
        if pg_progress.outbox_queued > 0:
            blocking.append(f"pg_outbox_queued={pg_progress.outbox_queued}")
        if pg_progress.outbox_processing > 0:
            blocking.append(f"pg_outbox_processing={pg_progress.outbox_processing}")
        if pg_progress.outbox_failed_retryable > 0:
            blocking.append(f"pg_outbox_failed_retryable={pg_progress.outbox_failed_retryable}")
        if pg_progress.outbox_dead_letter > 0:
            blocking.append(f"pg_outbox_dead_letter={pg_progress.outbox_dead_letter}")

    if neo4j_progress is not None:
        if neo4j_progress.missing_or_dirty_entities > 0:
            blocking.append(
                f"neo4j_missing_or_dirty_entities={neo4j_progress.missing_or_dirty_entities}"
            )
        if neo4j_progress.missing_or_dirty_edges > 0:
            blocking.append(
                f"neo4j_missing_or_dirty_edges={neo4j_progress.missing_or_dirty_edges}"
            )
        if neo4j_progress.groups_quarantined > 0:
            blocking.append(f"neo4j_groups_quarantined={neo4j_progress.groups_quarantined}")
        if neo4j_progress.groups_fenced_busy > 0:
            blocking.append(f"neo4j_groups_fenced_busy={neo4j_progress.groups_fenced_busy}")

    return LocalEmbeddingCutoverReport(
        profile_id=profile_id,
        ready=len(blocking) == 0,
        blocking_reasons=tuple(blocking),
        pg=pg_progress,
        neo4j=neo4j_progress,
    )
