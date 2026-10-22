"""CLI entry point: couchdb-session-memory-build.

Selects CouchDB transcript_session sessions whose projection_state is missing or
not PROJECTED, materializes each one and projects it to the RetiredIndexBridge session-memory
dataset.

Approval gate (fail-closed):
  Live (non-dry-run) runs require --approval <path> pointing to a JSON file with
  schema_version "agent_knowledge_live_approval.v1",
  operation "couchdb_session_memory_build", operator_approval.approved true,
  redaction_required true, timeout_seconds > 0, rollback_or_abort_criteria, and
  command.argv matching the actual argv passed to main().

Output: one JSON line (sorted keys) with keys: dry_run, failed, projected,
schema_version, selected, skipped.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

from agent_knowledge.qdrant_write_gateway_runtime import (
    QdrantMutationSource,
    QdrantWriteActivation,
    qdrant_write_activation_from_environment,
    qdrant_write_gateway_generation_from_environment,
)
from agent_knowledge.rag_ingress.pg_embedding_chunks import (
    PG_INPUT_POLICY_VERSION,
    TERMINAL_INPUT_REASONS,
)

BUILD_CLI_SCHEMA_VERSION = "couchdb_session_memory_build.v1"
BUILD_CLI_OPERATION = "couchdb_session_memory_build"
QDRANT_MUTATION_SOURCE = QdrantMutationSource.PROJECTION


def _build_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _blocked_source_hash_from_state(state: dict, backend: str) -> str:
    """Return the terminal-failure source hash for a top-level state document.

    This must be called on the raw projection_state, before ``backend_receipts``
    is merged over it. For an already-projected session the receipt's
    ``projection_status`` is ``projected`` and would otherwise hide the
    top-level failed attempt.
    """
    if backend != "postgres_pgvector":
        return ""
    from .document_model import ProjectionStatus

    attempt = (state.get("projection_attempt") or {}).get(backend) or {}
    if (
        attempt.get("policy_version") == PG_INPUT_POLICY_VERSION
        and str(attempt.get("status") or "") == ProjectionStatus.FAILED
        and str(attempt.get("reason") or "") in TERMINAL_INPUT_REASONS
        and str(attempt.get("source_hash") or "")
    ):
        return str(attempt["source_hash"])
    # Backward compatibility for states written before projection_attempt existed.
    if (
        str(state.get("projection_status") or "") == ProjectionStatus.FAILED
        and str(state.get("failure_reason") or "") in TERMINAL_INPUT_REASONS
        and str(state.get("failure_source_hash") or "")
    ):
        return str(state["failure_source_hash"])
    return ""


def _select_sessions_needing_projection(
    store, limit: int, *, project: str = "", provider: str = "", backend: str = "qdrant"
) -> list[dict]:
    """Return sessions whose projected revision is not the current source revision.

    When ``project``/``provider`` are set the selection is scoped to that subset so a
    scoped migration flow does not materialize out-of-scope sessions in the
    session-memory step.
    """
    from .document_model import (
        ProjectionStatus,
        SourceDocType,
        assert_hash_like,
        projection_state_doc_id,
        session_doc_id,
    )
    from .source_revision import resolve_active_source_revision_from_snapshot

    scope_selector = _scope_selector(project=project, provider=provider)
    hash_field = "backend_receipts" if backend == "postgres_pgvector" else "projected_source_hash"
    raw_states = store.find_by_type(
        SourceDocType.PROJECTION_STATE,
        fields=[
            "_id",
            "session_id_hash",
            "projection_status",
            "source_hash",
            "failure_reason",
            "failure_source_hash",
            "projection_attempt",
            hash_field,
        ],
        selector=scope_selector if backend == "postgres_pgvector" else {"projection_status": ProjectionStatus.PROJECTED, **scope_selector},
    )
    blocked_source_hashes: dict[str, str] = {}
    if backend == "postgres_pgvector":
        # Terminal input failures are policy outcomes, not transient faults. Leave the
        # session out of the queue while its source revision equals the blocked one; a
        # source change (or operator action) naturally re-queues it. Read the raw
        # state here: backend_receipts is merged below and can hide a failed attempt
        # behind the last successful projected receipt.
        for state in raw_states:
            session_id_hash = str(state.get("session_id_hash") or "")
            if (
                session_id_hash
                and str(state.get("_id") or "") == projection_state_doc_id(session_id_hash)
            ):
                blocked = _blocked_source_hash_from_state(state, backend)
                if blocked:
                    blocked_source_hashes[session_id_hash] = blocked
        states = [
            {**state, **(state.get("backend_receipts") or {}).get(backend, {})}
            for state in raw_states
        ]
    else:
        states = raw_states
    projected_source_hashes = {
        session_id_hash: str(state.get("projected_source_hash") or "")
        for state in states
        if (session_id_hash := str(state.get("session_id_hash") or ""))
        and str(state.get("_id") or "") == projection_state_doc_id(session_id_hash)
        and str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
    }
    selected: list[dict] = []
    canonical_session_ids: set[str] = set()
    current_only_session_ids: list[str] = []
    current_only_seen: set[str] = set()
    sessions = iter(
        _iter_by_type(
            store,
            SourceDocType.TRANSCRIPT_SESSION,
            fields=["_id", "session_id_hash", "provider", "project"],
            selector=scope_selector,
            page_size=max(1, limit) if limit > 0 else 10000,
        )
    )
    while limit <= 0 or len(selected) < limit:
        try:
            session = next(sessions)
        except StopIteration:
            break
        session_id_hash = str(session.get("session_id_hash") or "")
        if not session_id_hash:
            continue
        if str(session.get("_id") or "") != session_doc_id(session_id_hash):
            if session_id_hash not in current_only_seen:
                current_only_seen.add(session_id_hash)
                current_only_session_ids.append(session_id_hash)
            continue
        canonical_session_ids.add(session_id_hash)
        projected_source_hash = projected_source_hashes.get(session_id_hash, "")
        resolved = resolve_active_source_revision_from_snapshot(
            session_id_hash=session_id_hash,
            documents=store.find_by_session(session_id_hash=session_id_hash),
        )
        if not resolved.is_legacy_unpinned:
            if len(resolved.sessions) != 1:
                continue
            active_session = resolved.sessions[0]
            if (
                (project and str(active_session.get("project") or "") != project)
                or (provider and str(active_session.get("provider") or "") != provider)
            ):
                continue
        current_source_hash = resolved.source_hash
        if (
            not projected_source_hash
            or not current_source_hash
            or projected_source_hash != current_source_hash
        ):
            if blocked_source_hashes.get(session_id_hash) == current_source_hash:
                continue
            selected.append(session)

    # A corrective current-source import can legitimately have no canonical
    # legacy session row. Only then use the current session that a valid active
    # revision resolves from one complete session snapshot. A generic snapshot
    # is eligible only after that resolver proves it is the active revision;
    # generic/orphan copies are never used as legacy source discovery, and a
    # corrupt control document still propagates from the strict resolver.
    for session_id_hash in current_only_session_ids:
        if limit > 0 and len(selected) >= limit:
            break
        if session_id_hash in canonical_session_ids:
            continue
        source_snapshot = store.find_by_session(session_id_hash=session_id_hash)
        resolved = resolve_active_source_revision_from_snapshot(
            session_id_hash=session_id_hash,
            documents=source_snapshot,
        )
        if resolved.is_legacy_unpinned or len(resolved.sessions) != 1:
            continue
        session = resolved.sessions[0]
        if str(session.get("_id") or "") == session_doc_id(session_id_hash):
            continue
        if not str(session.get("source_snapshot_schema_version") or ""):
            try:
                assert_hash_like(
                    "current_source_scope", str(session.get("current_source_scope") or "")
                )
                assert_hash_like(
                    "supersedes_source_document_hash",
                    str(session.get("supersedes_source_document_hash") or ""),
                )
            except ValueError:
                continue
        if (
            (project and str(session.get("project") or "") != project)
            or (provider and str(session.get("provider") or "") != provider)
        ):
            continue
        projected_source_hash = projected_source_hashes.get(session_id_hash, "")
        if (
            not projected_source_hash
            or not resolved.source_hash
            or projected_source_hash != resolved.source_hash
        ):
            if blocked_source_hashes.get(session_id_hash) == resolved.source_hash:
                continue
            selected.append(session)
    return selected


# --- Incremental selection (change feed) -------------------------------------

_AFFECTED_CHANGE_PREFIXES = frozenset(
    {
        "transcript_session",
        "conversation_chunk",
        "tool_evidence_bundle",
        "coverage_manifest",
        "active_source_revision",
        "source_revision_member",
        "source_revision_manifest",
    }
)
_IGNORED_CHANGE_PREFIXES = frozenset(
    {"projection_state", "repo_usage_pattern", "retention_manifest"}
)
_SELECTION_CHECKPOINT_SCHEMA = "session_memory_selection_checkpoint.v1"
_DEFAULT_SELECTION_CHECKPOINT = "/app/state/session-memory-selection-checkpoint.json"


def _selection_checkpoint_path() -> str:
    return os.environ.get(
        "SESSION_MEMORY_SELECTION_CHECKPOINT", _DEFAULT_SELECTION_CHECKPOINT
    )


def _read_selection_checkpoint(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_selection_checkpoint(
    path: str,
    last_seq: str,
    *,
    pending_session_ids: list[str] | tuple[str, ...] = (),
    full_walk_in_progress: bool = False,
) -> None:
    import datetime

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload: dict = {
        "schema_version": _SELECTION_CHECKPOINT_SCHEMA,
        "pg_input_policy_version": PG_INPUT_POLICY_VERSION,
        "last_seq": str(last_seq or ""),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    # Chosen-but-not-yet-projected sessions are durable pending work. The change
    # feed only re-emits a document when it changes again, so a selected session
    # must stay listed here until its projection is verified; otherwise a failure
    # or restart between selection and completion silently loses that session.
    pending = [value for value in dict.fromkeys(str(v) for v in pending_session_ids) if value]
    if pending:
        payload["pending_session_ids"] = pending
    # A full walk that stopped at its cap has not examined the remaining tail.
    # Record that instead of claiming the database-wide sequence as seen, so the
    # next cycle repeats the full walk rather than switching to an incremental
    # change feed that will never emit the untouched tail.
    if full_walk_in_progress:
        payload["full_walk_in_progress"] = True
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)


def _evaluate_session_change(
    store, session_id_hash: str, *, project: str, provider: str
) -> tuple[bool, dict | None]:
    """Single-session mirror of ``_select_sessions_needing_projection`` (pg backend).

    Keep the branch conditions in sync with the full walk: canonical sessions use
    the resolver/active-session checks, current-only snapshots use the stricter
    successor checks, and blocked (secret-egress) revisions stay out of the queue.
    Returns ``(needs_projection, session_document_to_select)``.
    """
    from .document_model import (
        ProjectionStatus,
        assert_hash_like,
        projection_state_doc_id,
        session_doc_id,
    )
    from .source_revision import resolve_active_source_revision_from_snapshot

    documents = store.find_by_session(session_id_hash=session_id_hash)
    resolved = resolve_active_source_revision_from_snapshot(
        session_id_hash=session_id_hash, documents=documents
    )
    state_id = projection_state_doc_id(session_id_hash)
    state = store.get(state_id)
    merged: dict = {}
    blocked_source_hash = ""
    if isinstance(state, dict) and str(state.get("_id") or "") == state_id:
        blocked_source_hash = _blocked_source_hash_from_state(
            state, "postgres_pgvector"
        )
        merged = {
            **state,
            **((state.get("backend_receipts") or {}).get("postgres_pgvector") or {}),
        }
    projected_source_hash = ""
    if str(merged.get("projection_status") or "") == ProjectionStatus.PROJECTED:
        projected_source_hash = str(merged.get("projected_source_hash") or "")

    canonical = next(
        (
            doc
            for doc in documents
            if str(doc.get("_id") or "") == session_doc_id(session_id_hash)
        ),
        None,
    )
    if canonical is not None:
        if not resolved.is_legacy_unpinned:
            if len(resolved.sessions) != 1:
                return False, None
            active_session = resolved.sessions[0]
            if (
                (project and str(active_session.get("project") or "") != project)
                or (provider and str(active_session.get("provider") or "") != provider)
            ):
                return False, None
        current_source_hash = resolved.source_hash
    else:
        if resolved.is_legacy_unpinned or len(resolved.sessions) != 1:
            return False, None
        active_session = resolved.sessions[0]
        if str(active_session.get("_id") or "") == session_doc_id(session_id_hash):
            return False, None
        if not str(active_session.get("source_snapshot_schema_version") or ""):
            try:
                assert_hash_like(
                    "current_source_scope",
                    str(active_session.get("current_source_scope") or ""),
                )
                assert_hash_like(
                    "supersedes_source_document_hash",
                    str(active_session.get("supersedes_source_document_hash") or ""),
                )
            except ValueError:
                return False, None
        if (
            (project and str(active_session.get("project") or "") != project)
            or (provider and str(active_session.get("provider") or "") != provider)
        ):
            return False, None
        current_source_hash = resolved.source_hash

    if (
        not projected_source_hash
        or not current_source_hash
        or projected_source_hash != current_source_hash
    ):
        if blocked_source_hash and blocked_source_hash == current_source_hash:
            return False, None
        return True, canonical if canonical is not None else resolved.sessions[0]
    return False, None


def _select_sessions_incremental(
    store,
    *,
    checkpoint: dict,
    limit: int,
    project: str = "",
    provider: str = "",
    backend: str = "retired_index_bridge",
) -> tuple[list[dict], dict, int]:
    """Change-feed driven selection.

    Raises ``ValueError`` for any ambiguous window (unknown/deleted documents,
    mismatched ids, non-pg backends) so the caller can fall back to the full walk.
    The checkpoint only advances past rows that were fully examined; a limit cut
    leaves the unchecked tail for the next cycle.
    """
    if backend != "postgres_pgvector":
        raise ValueError("incremental selection supports postgres_pgvector only")
    since = str(checkpoint.get("last_seq") or "")
    if not since:
        raise ValueError("selection checkpoint missing last_seq")
    rows = store.changes_since(since=since, limit=10000)
    if not rows:
        return [], {"last_seq": since}, 0

    examined: set[str] = set()
    selected: list[dict] = []
    current_watermark = since
    for row in rows:
        seq = str(row.get("seq") or current_watermark)
        doc_id = str(row.get("id") or "")
        parts = doc_id.split(":")
        prefix = parts[0]
        candidate = parts[1] if len(parts) > 1 else ""
        if prefix in _IGNORED_CHANGE_PREFIXES:
            current_watermark = seq
            continue
        if (
            prefix not in _AFFECTED_CHANGE_PREFIXES
            or len(candidate) != 64
            or any(ch not in "0123456789abcdef" for ch in candidate)
        ):
            raise ValueError("unmappable change document")
        session_id_hash = f"sha256:{candidate}"
        if row.get("deleted"):
            raise ValueError("deleted source document requires full walk")
        if session_id_hash in examined:
            current_watermark = seq
            continue
        if limit > 0 and len(selected) >= limit:
            break
        document = store.get(doc_id)
        if not isinstance(document, dict):
            raise ValueError("changed document unavailable")
        if str(document.get("session_id_hash") or "") != session_id_hash:
            raise ValueError("change document session mismatch")
        examined.add(session_id_hash)
        needs, session = _evaluate_session_change(
            store, session_id_hash, project=project, provider=provider
        )
        if needs and session is not None:
            selected.append(session)
        current_watermark = seq
    return selected, {"last_seq": current_watermark}, len(rows)


def _pending_session_ids(checkpoint: dict) -> list[str]:
    values = checkpoint.get("pending_session_ids")
    if not isinstance(values, list):
        return []
    return [
        value
        for value in dict.fromkeys(str(item) for item in values if isinstance(item, str))
        if value.startswith("sha256:")
    ]


def _session_identity(session: dict) -> str:
    return str(session.get("session_id_hash") or "")


def _dedupe_sessions(sessions: list[dict]) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []
    for session in sessions:
        identity = _session_identity(session)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        deduped.append(session)
    return deduped


def _resolve_pending_sessions(
    store, pending_ids: list[str], *, project: str, provider: str
) -> list[dict]:
    """Re-select previously chosen sessions whose projection is still unfinished.

    Selection and completion are separate steps, so a pending id can describe a
    session that a later run already projected, one that is now policy-blocked,
    or one that genuinely still needs work. Re-evaluating each id keeps completed
    work out of the queue while preserving failed/interrupted work for retry. A
    transient evaluation error keeps the id pending instead of dropping it.
    """
    if not pending_ids:
        return []
    still_pending: list[dict] = []
    for session_id_hash in pending_ids:
        try:
            needs_projection, session = _evaluate_session_change(
                store, session_id_hash, project=project, provider=provider
            )
        except Exception:  # noqa: BLE001 - a read failure must not discard pending work
            still_pending.append({"session_id_hash": session_id_hash})
            continue
        if needs_projection and session is not None:
            still_pending.append(session)
    return still_pending


def _select_sessions_for_cycle(
    store,
    *,
    limit: int,
    project: str,
    provider: str,
    backend: str,
    mode: str,
    persist: bool = True,
) -> list[dict]:
    """Checkpoint-aware selection: incremental when possible, full walk otherwise.

    Two acknowledgement rules keep unprocessed work durable:

    * ``pending_session_ids`` carries every chosen-but-not-yet-verified session
      forward, because advancing the change-feed watermark past a selection is
      otherwise an acknowledgement of work that has not happened yet.
    * a full walk stopped by ``limit`` sets ``full_walk_in_progress`` instead of
      storing the database-wide sequence, so untouched sessions are re-scanned
      on the next cycle instead of being hidden behind an incremental feed.
    """

    normalized = (mode or "auto").strip().lower()
    if normalized not in {"auto", "incremental", "full"}:
        normalized = "auto"
    path = _selection_checkpoint_path()
    checkpoint = _read_selection_checkpoint(path)
    last_seq = str(checkpoint.get("last_seq") or "")
    full_walk_pending = bool(checkpoint.get("full_walk_in_progress"))
    pending_ids = _pending_session_ids(checkpoint)
    if backend == "postgres_pgvector" and checkpoint.get("pg_input_policy_version") != PG_INPUT_POLICY_VERSION:
        from .document_model import SourceDocType, projection_state_doc_id
        # Re-evaluate only failures whose input policy changed: terminal attempts
        # written under an older policy, plus historical PG DataError receipts.
        # A full scan would also activate unrelated, never-projected sources.
        stale_terminal = store.find_by_type(
            SourceDocType.PROJECTION_STATE,
            fields=["_id", "session_id_hash"],
            selector={
                "terminal_failure_policy_version": {"$lt": PG_INPUT_POLICY_VERSION},
                "terminal_failure_reason": {"$in": sorted(TERMINAL_INPUT_REASONS)},
            },
        )
        failures = store.find_by_type(
            SourceDocType.PROJECTION_STATE,
            fields=["_id", "session_id_hash"],
            selector={"backend_receipts.postgres_pgvector.failure_reason": "DataError"},
        )

        def _valid_projection_ids(rows: list[dict]) -> list[str]:
            return [
                str(row["session_id_hash"])
                for row in rows
                if row.get("session_id_hash")
                and row.get("_id") == projection_state_doc_id(str(row["session_id_hash"]))
            ]

        pending_ids = list(dict.fromkeys([
            *pending_ids,
            *_valid_projection_ids(stale_terminal),
            *_valid_projection_ids(failures),
        ]))
    pending_sessions = _resolve_pending_sessions(
        store, pending_ids, project=project, provider=provider
    )
    if limit <= 0:
        processed_pending = pending_sessions
        fresh_limit: int | None = None  # unlimited
    else:
        processed_pending = pending_sessions[:limit]
        fresh_limit = max(0, limit - len(processed_pending))
    remaining_pending = pending_sessions[len(processed_pending):]

    sessions: list[dict] | None = None
    next_seq = last_seq
    next_full_walk_pending = full_walk_pending

    if normalized != "full" and last_seq and not full_walk_pending and fresh_limit != 0:
        try:
            fresh, new_checkpoint, changed_count = _select_sessions_incremental(
                store,
                checkpoint=checkpoint,
                limit=fresh_limit or 0,
                project=project,
                provider=provider,
                backend=backend,
            )
        except Exception as exc:  # noqa: BLE001 - fail-safe fallback to the full walk
            print(
                f"[selection] incremental fallback ({type(exc).__name__})",
                file=sys.stderr,
            )
        else:
            sessions = _dedupe_sessions(processed_pending + fresh)
            next_seq = str(new_checkpoint.get("last_seq") or last_seq)
            next_full_walk_pending = False
            print(
                f"[selection] mode=incremental changed={changed_count} "
                f"selected={len(fresh)} pending_recovered={len(processed_pending)}",
                file=sys.stderr,
            )

    if sessions is None and fresh_limit == 0:
        # Pending work already fills this run's cap: leave the change-feed
        # watermark and any in-progress full walk exactly where they were.
        sessions = processed_pending
        print(
            f"[selection] mode=pending-preserved selected={len(sessions)} "
            f"pending_recovered={len(processed_pending)}",
            file=sys.stderr,
        )
    elif sessions is None:
        sequence_before = ""
        try:
            sequence_before = str(store.read_change_sequence())
        except Exception:  # noqa: BLE001 - checkpoint is best-effort observability
            sequence_before = ""
        walk_limit = 0 if fresh_limit is None else fresh_limit
        fresh = _select_sessions_needing_projection(
            store, limit=walk_limit, project=project, provider=provider, backend=backend
        )
        sessions = _dedupe_sessions(processed_pending + fresh)
        capped = fresh_limit is not None and fresh_limit > 0 and len(fresh) >= fresh_limit
        next_full_walk_pending = capped
        next_seq = "" if capped else sequence_before
        print(
            f"[selection] mode=full selected={len(fresh)} "
            f"pending_recovered={len(processed_pending)} capped={str(capped).lower()}",
            file=sys.stderr,
        )

    # Pending debt can be larger than this run's cap: keep every still-unfinished
    # id, including ids that were not reached because pending work filled the cap.
    durable_pending = _session_identity_many(remaining_pending) + _session_identity_many(sessions)
    if persist:
        try:
            _write_selection_checkpoint(
                path,
                next_seq,
                pending_session_ids=durable_pending,
                full_walk_in_progress=next_full_walk_pending,
            )
        except OSError:
            pass
    return sessions


def _session_identity_many(sessions: list[dict]) -> list[str]:
    return [identity for identity in (_session_identity(s) for s in sessions) if identity]


def _scope_selector(*, project: str, provider: str) -> dict[str, str]:
    selector = {}
    if project:
        selector["project"] = project
    if provider:
        selector["provider"] = provider
    return selector


def _iter_by_type(store, doc_type: str, *, fields: list[str], selector: dict, page_size: int = 10000):
    iterator = getattr(store, "iter_by_type", None)
    if callable(iterator):
        return iterator(doc_type, fields=fields, selector=selector, page_size=page_size)
    try:
        return iter(store.find_by_type(doc_type, fields=fields, selector=selector, page_size=page_size))
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        docs = store.find_by_type(doc_type, fields=fields)
        return (
            doc
            for doc in docs
            if all(doc.get(key) == value for key, value in selector.items())
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge couchdb-session-memory-build",
        description="Build CouchDB->session-memory live pipeline.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report selection counts; no RetiredIndexBridge writes.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum sessions to process (0 = unlimited).")
    parser.add_argument("--project", default="", help="Scope selection to this project (empty = all).")
    parser.add_argument("--provider", default="", help="Scope selection to this provider (empty = all).")
    parser.add_argument("--approval", default="", help="Path to live-approval JSON (required for non-dry-run).")
    parser.add_argument("--dataset-name", default="session-memory", help="RetiredIndexBridge dataset name (default: session-memory).")
    parser.add_argument("--retired-index-bridge-url", default="", help="RetiredIndexBridge base URL (overrides RETIRED_INDEX_BRIDGE_URL env).")
    parser.add_argument(
        "--retired-index-bridge-token-env",
        default="RETIRED_INDEX_BRIDGE_API_KEY",
        help="(무시됨) RetiredIndexBridge 토큰은 항상 RETIRED_INDEX_BRIDGE_API_KEY env에서만 읽는다.",
    )

    args = parser.parse_args(argv)

    # Reconstruct the effective argv for approval matching.
    effective_argv = list(sys.argv[1:] if argv is None else argv)

    # --- Approval gate (fail-closed) ----------------------------------------
    if not args.dry_run:
        from ..session_memory.native_memory_sync_approval import (
            ApprovalError,
            validate_memory_enqueue_approval,
        )

        try:
            validate_memory_enqueue_approval(
                args.approval or None,
                operation=BUILD_CLI_OPERATION,
                command_argv=effective_argv,
            )
        except ApprovalError as exc:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "approval_rejected",
                        "reason": str(exc),
                        "dry_run": False,
                        "selected": 0,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2

    # --- Store connection ----------------------------------------------------
    couchdb_url = os.environ.get("COUCHDB_URL", "")
    couchdb_user = os.environ.get("COUCHDB_USER", "")
    couchdb_password = os.environ.get("COUCHDB_PASSWORD", "")
    couchdb_db = os.environ.get("COUCHDB_DB", "transcript_source")

    if not couchdb_url:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "error": "env_missing",
                    "reason": "COUCHDB_URL is required",
                    "dry_run": args.dry_run,
                    "selected": 0,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                sort_keys=True,
            )
        )
        return 2

    from .couchdb_http_store import CouchDBHttpSourceStore

    auth_header = _build_auth_header(couchdb_user, couchdb_password) if couchdb_user else ""
    store = CouchDBHttpSourceStore(
        base_url=couchdb_url,
        db=couchdb_db,
        auth_header=auth_header,
    )

    # --- Backend Resolution -------------------------------------------------
    backend = os.environ.get("SESSION_MEMORY_PROJECTION_BACKEND", "retired_index_bridge").strip().lower()

    # --- Session selection --------------------------------------------------
    sessions = _select_sessions_for_cycle(
        store,
        limit=args.limit,
        project=str(args.project or ""),
        provider=str(args.provider or ""),
        backend=backend,
        mode=os.environ.get("SESSION_MEMORY_SELECTION_MODE", "auto"),
        persist=not args.dry_run,
    )
    selected_count = len(sessions)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "dry_run": True,
                    "selected": selected_count,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                sort_keys=True,
            )
        )
        return 0

    # --- Projector construction (live run only) ------------------------------
    from .session_memory_materializer import materialize_and_project
    # Fail-closed: 오타 등으로 알 수 없는 backend가 들어오면 retired_index_bridge로 조용히
    # fallback하지 않고 명시적으로 거부한다.
    if backend not in {"retired_index_bridge", "qdrant", "postgres_pgvector"}:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "error": "env_invalid",
                    "reason": (
                        "SESSION_MEMORY_PROJECTION_BACKEND는 'retired_index_bridge', 'qdrant', 또는 'postgres_pgvector'여야 한다"
                    ),
                    "dry_run": False,
                    "selected": 0,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                sort_keys=True,
            )
        )
        return 2
    if backend == "qdrant":
        # Qdrant-direct write path (retired-index-bridge-free): project each session-memory
        # straight into the Qdrant searchable mirror as the CANONICAL target. No
        # RetiredIndexBridge URL/token required. A submit failure marks the projection FAILED
        # (retried next run), not best-effort. mirror_sink stays None -- the projector
        # IS the Qdrant writer, so there is no separate best-effort forward hook.
        projector = _build_qdrant_projector(os.environ)
        if projector is None:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "env_missing",
                        "reason": "QDRANT_URL (and a reachable mirror collection) is required for the qdrant projection backend",
                        "dry_run": False,
                        "selected": selected_count,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2
        mirror_sink = None
    elif backend == "postgres_pgvector":
        try:
            projector = _build_pg_projector(os.environ)
        except Exception:
            print(json.dumps({
                "schema_version": BUILD_CLI_SCHEMA_VERSION,
                "error": "pg_projector_unavailable",
                "dry_run": False, "selected": selected_count,
                "projected": 0, "failed": 0, "skipped": 0,
            }, sort_keys=True))
            return 2
        if projector is None:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "env_missing",
                        "reason": "PGVECTOR_DSN is required for the postgres_pgvector projection backend",
                        "dry_run": False,
                        "selected": selected_count,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2
        mirror_sink = None
    else:
        index_url = args.retired_index_bridge_url or os.environ.get("RETIRED_INDEX_BRIDGE_URL", "")
        # Repo guardrail: RetiredIndexBridge 토큰은 RETIRED_INDEX_BRIDGE_API_KEY 하나만 사용한다. args.retired_index_bridge_token_env로
        # 임의의 env 이름을 secret 소스로 받지 않도록 하드코딩한다.
        bearer_token = os.environ.get("RETIRED_INDEX_BRIDGE_API_KEY", "")
        if not index_url or not bearer_token:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "env_missing",
                        "reason": "index_url and token are required for live runs",
                        "dry_run": False,
                        "selected": selected_count,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2

        from .index_projector import RetiredIndexBridgeSessionMemoryProjector

        projector = RetiredIndexBridgeSessionMemoryProjector(
            index_url=index_url,
            bearer_token=bearer_token,
            dataset_name=args.dataset_name,
        )
        # Optional best-effort Qdrant forward mirror ALONGSIDE RetiredIndexBridge (legacy dual
        # path). Off unless MIRROR_DUAL_WRITE=1 AND QDRANT_URL are set; a mirror
        # misconfig yields a None sink and NEVER blocks the canonical RetiredIndexBridge projection.
        mirror_sink = _build_forward_mirror_sink(os.environ)

    projected = 0
    failed = 0
    skipped = 0

    try:
        for session in sessions:
            session_id_hash = str(session.get("session_id_hash") or "")
            if not session_id_hash:
                skipped += 1
                continue
            try:
                result = materialize_and_project(
                    session_id_hash=session_id_hash,
                    store=store,
                    projector=projector,
                    mirror_sink=mirror_sink,
                    backend=backend,
                )
                projection = result.get("projection") or {}
                status = str(projection.get("status") or "")
                if status == "projected":
                    projected += 1
                elif status == "pending" or not result.get("fully_materialized"):
                    skipped += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    finally:
        _close_if_supported(mirror_sink)
        _close_if_supported(projector)

    print(
        json.dumps(
            {
                "schema_version": BUILD_CLI_SCHEMA_VERSION,
                "dry_run": False,
                "selected": selected_count,
                "projected": projected,
                "failed": failed,
                "skipped": skipped,
            },
            sort_keys=True,
        )
    )
    return 0 if failed == 0 else 1


def _close_if_supported(resource) -> None:
    closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        return


def _build_forward_mirror_sink(environ):
    """Build the best-effort Qdrant forward mirror sink, or None when disabled.

    Enabled only when ``MIRROR_DUAL_WRITE=1`` AND ``QDRANT_URL`` are set. Reuses the
    OpenAI-compatible embedding provider (``LLM_BRAIN_EMBEDDING_*``) and a passthrough
    normalizer (session-memory bodies are already redacted). ``ensure_collection`` is
    False: the forward write targets the existing live mirror collection and must NOT
    create one implicitly. Any construction failure (missing dep, bad url, absent
    collection) returns None so a mirror misconfig can never block the canonical
    CouchDB session-memory builder.
    """

    if str(environ.get("MIRROR_DUAL_WRITE") or "").strip() != "1":
        return None
    try:
        activation = qdrant_write_activation_from_environment(environ)
        if activation is QdrantWriteActivation.FOUNDATION_INACTIVE:
            return None
        url = str(environ.get("QDRANT_URL") or "").strip()
        if not url:
            return None
        from ..rag_ingress.qdrant_backfill import QdrantSessionMemoryMirrorSink
        from ..rag_ingress.qdrant_docling_mirror import (
            DEFAULT_COLLECTION_NAME,
            FOUNDATION_DIRECT_WRITE_CONTRACT,
            PassthroughMarkdownNormalizer,
            build_remote_qdrant_docling_mirror_adapter,
            build_remote_qdrant_docling_sidecar_adapter,
        )
        from ..rag_ingress.qdrant_embedding import build_openai_embedding_provider

        collection = str(environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION_NAME).strip()
        common = {
            "collection_name": collection,
            "embedding_provider": build_openai_embedding_provider(environ=environ),
            "normalizer": PassthroughMarkdownNormalizer(),
        }
        if activation is QdrantWriteActivation.FOUNDATION_DIRECT:
            adapter = build_remote_qdrant_docling_mirror_adapter(
                url=url,
                direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
                **common,
            )
        else:
            adapter = build_remote_qdrant_docling_sidecar_adapter(
                read_url=url,
                read_api_key_path=str(environ.get("QDRANT_READ_API_KEY_FILE") or ""),
                gateway_endpoint=str(
                    environ.get("QDRANT_WRITE_GATEWAY_ENDPOINT") or ""
                ),
                gateway_token_path=str(
                    environ.get("QDRANT_WRITE_GATEWAY_TOKEN_FILE") or ""
                ),
                gateway_ca_path=str(
                    environ.get("QDRANT_WRITE_GATEWAY_CA_FILE") or ""
                ),
                gateway_generation=qdrant_write_gateway_generation_from_environment(
                    environ
                ),
                source=QDRANT_MUTATION_SOURCE,
                **common,
            )
        return QdrantSessionMemoryMirrorSink(adapter)
    except Exception:
        # Mirror misconfig must never block the canonical builder.
        return None


def _build_qdrant_projector(environ):
    """Build the Qdrant-direct session-memory projector, or None when unbuildable.

    Requires ``QDRANT_URL`` and a reachable, already-existing mirror collection
    (``ensure_collection=False`` -- never create implicitly). Reuses the
    OpenAI-compatible embedding provider (``LLM_BRAIN_EMBEDDING_*``) and the
    passthrough normalizer. Returns None on missing url / unbuildable adapter so the
    caller fails closed (the qdrant backend then reports env_missing and projects
    nothing this run rather than silently dropping sessions).
    """

    try:
        activation = qdrant_write_activation_from_environment(environ)
        if activation is QdrantWriteActivation.FOUNDATION_INACTIVE:
            return None
        url = str(environ.get("QDRANT_URL") or "").strip()
        if not url:
            return None
        from ..rag_ingress.qdrant_backfill import (
            QdrantSessionMemoryMirrorSink,
            QdrantSessionMemoryProjector,
        )
        from ..rag_ingress.qdrant_docling_mirror import (
            DEFAULT_COLLECTION_NAME,
            FOUNDATION_DIRECT_WRITE_CONTRACT,
            PassthroughMarkdownNormalizer,
            build_remote_qdrant_docling_mirror_adapter,
            build_remote_qdrant_docling_sidecar_adapter,
        )
        from ..rag_ingress.qdrant_embedding import build_openai_embedding_provider

        collection = str(environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION_NAME).strip()
        common = {
            "collection_name": collection,
            "embedding_provider": build_openai_embedding_provider(environ=environ),
            "normalizer": PassthroughMarkdownNormalizer(),
        }
        if activation is QdrantWriteActivation.FOUNDATION_DIRECT:
            adapter = build_remote_qdrant_docling_mirror_adapter(
                url=url,
                direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
                **common,
            )
        else:
            adapter = build_remote_qdrant_docling_sidecar_adapter(
                read_url=url,
                read_api_key_path=str(environ.get("QDRANT_READ_API_KEY_FILE") or ""),
                gateway_endpoint=str(
                    environ.get("QDRANT_WRITE_GATEWAY_ENDPOINT") or ""
                ),
                gateway_token_path=str(
                    environ.get("QDRANT_WRITE_GATEWAY_TOKEN_FILE") or ""
                ),
                gateway_ca_path=str(
                    environ.get("QDRANT_WRITE_GATEWAY_CA_FILE") or ""
                ),
                gateway_generation=qdrant_write_gateway_generation_from_environment(
                    environ
                ),
                source=QDRANT_MUTATION_SOURCE,
                **common,
            )
        return QdrantSessionMemoryProjector(QdrantSessionMemoryMirrorSink(adapter))
    except Exception:
        return None


def _build_pg_projector(environ):
    dsn = (
        environ.get("NEURON_LBRAIN_PGVECTOR_DSN", "")
        or environ.get("LLM_BRAIN_PGVECTOR_DSN", "")
        or environ.get("NEURON_LEDGER_PG_DSN", "")
    )
    if not dsn:
        return None
    from ..rag_ingress.pg_backfill import PgSessionMemoryProjector
    from ..postgres_store.pgvector_store import PgVectorStore
    from ..rag_ingress.qdrant_embedding import build_openai_embedding_provider, resolve_embedding_config
    from ..rag_ingress.pg_embedding_chunks import BoundedPgEmbedding

    store = PgVectorStore(dsn=dsn, pgvector_version=environ.get("LLM_BRAIN_PGVECTOR_VERSION", "0.8.0"))
    provider = build_openai_embedding_provider(environ=environ)
    try:
        chunks = BoundedPgEmbedding(
            provider,
            cache_path=environ.get("PG_EMBEDDING_CHUNK_CACHE", "/app/state/pg-embedding-chunks.sqlite3"),
            endpoint_scope=str(resolve_embedding_config(environ)["base_url"]),
            cycle_budget=int(environ.get("PG_EMBEDDING_CHUNKS_PER_CYCLE", "192")),
            session_budget=int(environ.get("PG_EMBEDDING_CHUNKS_PER_SESSION", "64")),
        )
        return PgSessionMemoryProjector(store=store, embed_provider=provider, chunk_embedder=chunks)
    except Exception:
        _close_if_supported(provider)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
