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

Output: one JSON line (sorted keys) with keys: dry_run, failed,
identity_conflicts_auto_resolved, projected, schema_version, selected, skipped.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from types import SimpleNamespace

from agent_knowledge.rag_ingress.pg_embedding_chunks import (
    INCONSISTENT_HERMES_PROJECT_SCOPE,
    PG_INPUT_POLICY_VERSION,
    SOURCE_PAYLOAD_TOO_LARGE,
    TERMINAL_INPUT_REASONS,
)
from .couchdb_http_store import CouchDBResponseTooLarge

BUILD_CLI_SCHEMA_VERSION = "couchdb_session_memory_build.v1"
BUILD_CLI_OPERATION = "couchdb_session_memory_build"
# Pending debt must not monopolise every cycle: a session that stays pending
# across this many selections is dropped from the pending set so fresh work can
# proceed (no-progress guard). Its change-feed entry re-enters if the source
# still differs from the projected revision.
MAX_PENDING_ATTEMPTS = 6


def _build_auth_header(user: str, password: str) -> str:
    if not user:
        return ""
    from .couchdb_http_store import basic_auth_header

    return basic_auth_header(user=user, password=password)


def _pg_receipt_lane(embedding_profile: str) -> str:
    from ..model_connectors import LOCAL_EMBEDDING_PROFILE_ID

    return (
        "postgres_pgvector_local"
        if embedding_profile == LOCAL_EMBEDDING_PROFILE_ID
        else "postgres_pgvector"
    )


def _pg_receipt(state: dict, *, embedding_profile: str) -> dict:
    receipts = state.get("backend_receipts") or {}
    if not isinstance(receipts, dict):
        return {}
    lane = _pg_receipt_lane(embedding_profile)
    lane_receipts = receipts.get(lane) or {}
    if lane == "postgres_pgvector_local":
        receipt = lane_receipts.get(embedding_profile) if isinstance(lane_receipts, dict) else None
    else:
        receipt = lane_receipts
    return receipt if isinstance(receipt, dict) else {}


def _pg_receipt_representation(state: dict, *, embedding_profile: str) -> str:
    receipt = _pg_receipt(state, embedding_profile=embedding_profile)
    return "segment_v1" if receipt.get("receipt_version") == 4 else "session_v1"


def _blocked_source_hash_from_state(
    state: dict,
    backend: str,
    *,
    embedding_profile: str = "",
    representation: str = "session_v1",
) -> str:
    """Return the terminal-failure source hash for a top-level state document.

    This must be called on the raw projection_state, before ``backend_receipts``
    is merged over it. For an already-projected session the receipt's
    ``projection_status`` is ``projected`` and would otherwise hide the
    top-level failed attempt.
    """
    if backend != "postgres_pgvector":
        return ""
    if _pg_receipt_representation(
        state, embedding_profile=embedding_profile
    ) != representation:
        return ""
    from .document_model import ProjectionStatus

    lane = _pg_receipt_lane(embedding_profile)
    attempt = (state.get("projection_attempt") or {}).get(lane) or {}
    if (
        attempt.get("policy_version") == PG_INPUT_POLICY_VERSION
        and str(attempt.get("status") or "") == ProjectionStatus.FAILED
        and str(attempt.get("reason") or "") in TERMINAL_INPUT_REASONS
        and str(attempt.get("source_hash") or "")
    ):
        return str(attempt["source_hash"])
    if lane != "postgres_pgvector_local":
        # Backward compatibility for states written before projection_attempt existed.
        if (
            str(state.get("projection_status") or "") == ProjectionStatus.FAILED
            and str(state.get("failure_reason") or "") in TERMINAL_INPUT_REASONS
            and str(state.get("failure_source_hash") or "")
        ):
            return str(state["failure_source_hash"])
    return ""


def _handle_unresolvable_session_in_selection(
    *,
    store,
    session_id_hash: str,
    session_doc: dict,
    backend: str,
    embedding_profile: str = "",
    exc: Exception,
) -> None:
    """Safely record terminal failure or attempt audit when a session cannot be resolved.

    Prevents unbounded CronJob crash-loops when encountering documents that exceed
    the hard response size cap or have unrecoverable structure, while protecting
    subsequent sessions in the cycle from starvation.
    """
    from .document_model import ProjectionStatus, SourceDocType, projection_state_doc_id
    from ..rag_ingress.pg_embedding_chunks import (
        PG_INPUT_POLICY_VERSION,
        SOURCE_PAYLOAD_TOO_LARGE,
    )

    if backend != "postgres_pgvector":
        return

    state_id = projection_state_doc_id(session_id_hash)
    try:
        existing = store.get(state_id)
        if not isinstance(existing, dict):
            existing = {
                "_id": state_id,
                "doc_type": SourceDocType.PROJECTION_STATE,
                "session_id_hash": session_id_hash,
            }
        lane = _pg_receipt_lane(embedding_profile)
        attempts = dict(existing.get("projection_attempt") or {})
        source_hash = str(session_doc.get("source_hash") or "")
        reason = (
            SOURCE_PAYLOAD_TOO_LARGE
            if getattr(exc, "code", "") == "couchdb_response_too_large"
            else type(exc).__name__
        )
        attempts[lane] = {
            "status": ProjectionStatus.FAILED,
            "reason": reason,
            "source_hash": source_hash,
            "policy_version": PG_INPUT_POLICY_VERSION,
        }
        updated = {**existing, "projection_attempt": attempts}
        if source_hash:
            updated["terminal_failure_policy_version"] = PG_INPUT_POLICY_VERSION
            updated["terminal_failure_source_hash"] = source_hash
            updated["terminal_failure_reason"] = reason

        put_fn = getattr(store, "put", None)
        if callable(put_fn):
            put_fn(updated)
    except Exception:
        # Best-effort audit write during selection must never break the cycle
        pass


def _select_sessions_needing_projection(
    store,
    limit: int,
    *,
    project: str = "",
    provider: str = "",
    backend: str = "postgres_pgvector",
    embedding_profile: str = "",
    representation: str = "session_v1",
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
    raw_states = store.find_by_type(
        SourceDocType.PROJECTION_STATE,
        fields=[
            "_id",
            "session_id_hash",
            "projection_status",
            "source_hash",
            "projected_source_hash",
            "failure_reason",
            "failure_source_hash",
            "projection_attempt",
            "backend_receipts",
        ],
        selector=scope_selector if backend == "postgres_pgvector" else {"projection_status": ProjectionStatus.PROJECTED, **scope_selector},
    )
    blocked_source_hashes: dict[str, str] = {}
    blocked_oversized_sessions: set[str] = set()
    projected_source_hashes: dict[str, str] = {}
    projected_representations: dict[str, str] = {}

    for state in raw_states:
        session_id_hash = str(state.get("session_id_hash") or "")
        if not session_id_hash or str(state.get("_id") or "") != projection_state_doc_id(session_id_hash):
            continue

        if backend == "postgres_pgvector":
            blocked = _blocked_source_hash_from_state(
                state,
                backend,
                embedding_profile=embedding_profile,
                representation=representation,
            )
            if blocked:
                blocked_source_hashes[session_id_hash] = blocked

            lane = _pg_receipt_lane(embedding_profile)
            attempt = (state.get("projection_attempt") or {}).get(lane) or {}
            if (
                str(attempt.get("status") or "") == ProjectionStatus.FAILED
                and str(attempt.get("reason") or "") == SOURCE_PAYLOAD_TOO_LARGE
            ):
                blocked_oversized_sessions.add(session_id_hash)

            receipt = _pg_receipt(state, embedding_profile=embedding_profile)
            effective_state = {**state, **receipt} if receipt else state
        else:
            effective_state = state

        if str(effective_state.get("projection_status") or "") == ProjectionStatus.PROJECTED:
            projected_source_hashes[session_id_hash] = str(
                effective_state.get("projected_source_hash") or ""
            )
            projected_representations[session_id_hash] = _pg_receipt_representation(
                effective_state, embedding_profile=embedding_profile
            )
    del raw_states

    selected: list[dict] = []
    canonical_session_ids: set[str] = set()
    current_only_session_ids: list[str] = []
    current_only_seen: set[str] = set()
    sessions = iter(
        _iter_by_type(
            store,
            SourceDocType.TRANSCRIPT_SESSION,
            fields=["_id", "session_id_hash", "provider", "project", "source_hash"],
            selector=scope_selector,
            page_size=max(1, limit) if limit > 0 else 1000,
        )
    )
    while limit <= 0 or len(selected) < limit:
        try:
            session = next(sessions)
        except StopIteration:
            break
        session_id_hash = str(session.get("session_id_hash") or "")
        if not session_id_hash or session_id_hash in blocked_oversized_sessions:
            continue
        if str(session.get("_id") or "") != session_doc_id(session_id_hash):
            if session_id_hash not in current_only_seen:
                current_only_seen.add(session_id_hash)
                current_only_session_ids.append(session_id_hash)
            continue
        canonical_session_ids.add(session_id_hash)
        projected_source_hash = projected_source_hashes.get(session_id_hash, "")
        try:
            documents = store.find_by_session(session_id_hash=session_id_hash)
            resolved = resolve_active_source_revision_from_snapshot(
                session_id_hash=session_id_hash,
                documents=documents,
            )
        except CouchDBResponseTooLarge as exc:
            _handle_unresolvable_session_in_selection(
                store=store,
                session_id_hash=session_id_hash,
                session_doc=session,
                backend=backend,
                embedding_profile=embedding_profile,
                exc=exc,
            )
            continue
        if not resolved.is_legacy_unpinned:
            if len(resolved.sessions) != 1:
                del resolved
                continue
            active_session = resolved.sessions[0]
            if (
                (project and str(active_session.get("project") or "") != project)
                or (provider and str(active_session.get("provider") or "") != provider)
            ):
                del resolved
                continue
        current_source_hash = resolved.source_hash
        if (
            not projected_source_hash
            or not current_source_hash
            or projected_source_hash != current_source_hash
            or (
                backend == "postgres_pgvector"
                and projected_representations.get(session_id_hash, "session_v1")
                != representation
            )
        ):
            if blocked_source_hashes.get(session_id_hash) == current_source_hash:
                del resolved
                continue
            selected.append(session)
        del resolved

    # A corrective current-source import can legitimately have no canonical
    # legacy session row. Only then use the current session that a valid active
    # revision resolves from one complete session snapshot. A generic snapshot
    # is eligible only after that resolver proves it is the active revision;
    # generic/orphan copies are never used as legacy source discovery, and a
    # corrupt control document still propagates from the strict resolver.
    for session_id_hash in current_only_session_ids:
        if limit > 0 and len(selected) >= limit:
            break
        if session_id_hash in canonical_session_ids or session_id_hash in blocked_oversized_sessions:
            continue
        try:
            source_snapshot = store.find_by_session(session_id_hash=session_id_hash)
            resolved = resolve_active_source_revision_from_snapshot(
                session_id_hash=session_id_hash,
                documents=source_snapshot,
            )
        except CouchDBResponseTooLarge as exc:
            _handle_unresolvable_session_in_selection(
                store=store,
                session_id_hash=session_id_hash,
                session_doc={"session_id_hash": session_id_hash},
                backend=backend,
                embedding_profile=embedding_profile,
                exc=exc,
            )
            continue
        if resolved.is_legacy_unpinned or len(resolved.sessions) != 1:
            del resolved
            continue
        session = resolved.sessions[0]
        if str(session.get("_id") or "") == session_doc_id(session_id_hash):
            del resolved
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
                del resolved
                continue
        if (
            (project and str(session.get("project") or "") != project)
            or (provider and str(session.get("provider") or "") != provider)
        ):
            del resolved
            continue
        projected_source_hash = projected_source_hashes.get(session_id_hash, "")
        if (
            not projected_source_hash
            or not resolved.source_hash
            or projected_source_hash != resolved.source_hash
            or (
                backend == "postgres_pgvector"
                and projected_representations.get(session_id_hash, "session_v1")
                != representation
            )
        ):
            if blocked_source_hashes.get(session_id_hash) == resolved.source_hash:
                del resolved
                continue
            selected.append(session)
        del resolved

    del projected_source_hashes, blocked_source_hashes, projected_representations
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
    embedding_profile: str = "",
    representation: str = "session_v1",
    pending_session_ids: list[str] | tuple[str, ...] = (),
    full_walk_in_progress: bool = False,
    pending_attempts: dict[str, int] | None = None,
) -> None:
    import datetime

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload: dict = {
        "schema_version": _SELECTION_CHECKPOINT_SCHEMA,
        "pg_input_policy_version": PG_INPUT_POLICY_VERSION,
        "embedding_profile": str(embedding_profile or ""),
        "representation": str(representation or "session_v1"),
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
    if pending_attempts:
        payload["pending_session_attempts"] = {
            str(key): int(value) for key, value in pending_attempts.items() if str(key)
        }
    # A full walk that stopped at its cap has not examined the remaining tail.
    # Record that instead of claiming the database-wide sequence as seen, so the
    # next cycle repeats the full walk rather than switching to an incremental
    # change feed that will never emit the untouched tail.
    if full_walk_in_progress:
        payload["full_walk_in_progress"] = True
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)


def _evaluate_session_change(
    store,
    session_id_hash: str,
    *,
    project: str,
    provider: str,
    embedding_profile: str = "",
    representation: str = "session_v1",
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
            state,
            "postgres_pgvector",
            embedding_profile=embedding_profile,
            representation=representation,
        )
        merged = {
            **state,
            **_pg_receipt(state, embedding_profile=embedding_profile),
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
        or _pg_receipt_representation(
            merged, embedding_profile=embedding_profile
        ) != representation
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
    backend: str = "postgres_pgvector",
    embedding_profile: str = "",
    representation: str = "session_v1",
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
            store,
            session_id_hash,
            project=project,
            provider=provider,
            embedding_profile=embedding_profile,
            representation=representation,
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


def _apply_pending_attempt_cap(
    sessions: list[dict], attempts: dict, *, limit: int = MAX_PENDING_ATTEMPTS
) -> tuple[list[dict], dict, int]:
    """Keep pending sessions fair: drop one that never finishes after N tries."""

    surviving: list[dict] = []
    updated: dict[str, int] = {}
    dropped = 0
    for session in sessions:
        identity = _session_identity(session)
        if not identity:
            continue
        seen = int(attempts.get(identity) or 0) + 1
        if seen > limit:
            dropped += 1
            continue
        updated[identity] = seen
        surviving.append(session)
    return surviving, updated, dropped


def _resolve_pending_sessions(
    store,
    pending_ids: list[str],
    *,
    project: str,
    provider: str,
    embedding_profile: str = "",
    representation: str = "session_v1",
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
                store,
                session_id_hash,
                project=project,
                provider=provider,
                embedding_profile=embedding_profile,
                representation=representation,
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
    embedding_profile: str = "",
    representation: str = "session_v1",
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
    if representation not in {"session_v1", "segment_v1"}:
        raise ValueError("unsupported session-memory representation")
    last_seq = str(checkpoint.get("last_seq") or "")
    full_walk_pending = bool(checkpoint.get("full_walk_in_progress"))
    if str(checkpoint.get("embedding_profile") or "") != str(embedding_profile or ""):
        full_walk_pending = True
    checkpoint_representation = str(checkpoint.get("representation") or "session_v1")
    if checkpoint_representation != representation:
        full_walk_pending = True
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
        store,
        pending_ids,
        project=project,
        provider=provider,
        embedding_profile=embedding_profile,
        representation=representation,
    )
    pending_attempts = dict(checkpoint.get("pending_session_attempts") or {})
    pending_sessions, pending_attempts, dropped_pending = _apply_pending_attempt_cap(
        pending_sessions, pending_attempts
    )
    if dropped_pending:
        print(
            f"[selection] pending_attempts_exhausted dropped={dropped_pending}",
            file=sys.stderr,
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
                embedding_profile=embedding_profile,
                representation=representation,
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
            store,
            limit=walk_limit,
            project=project,
            provider=provider,
            backend=backend,
            embedding_profile=embedding_profile,
            representation=representation,
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
                embedding_profile=embedding_profile,
                representation=representation,
                pending_attempts={
                    key: value
                    for key, value in pending_attempts.items()
                    if key in set(durable_pending)
                },
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
    parser.add_argument("--dataset-name", default="session-memory", help="Projection dataset name (default: session-memory).")
    parser.add_argument(
        "--session-id-hash",
        action="append",
        default=[],
        dest="session_id_hashes",
        help="Project only this session_id_hash (sha256:<hex>, repeatable; event-driven path). Skips checkpoint selection and never writes the checkpoint.",
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
    backend = os.environ.get("SESSION_MEMORY_PROJECTION_BACKEND", "postgres_pgvector").strip().lower()
    representation = (
        os.environ.get("SESSION_MEMORY_REPRESENTATION", "session_v1").strip().lower()
        or "session_v1"
    )
    embedding_profile = ""
    if backend == "postgres_pgvector":
        try:
            from ..rag_ingress.pg_backfill import validated_pg_embedding_profile
            from ..rag_ingress.embedding_provider import resolve_embedding_config

            cfg = resolve_embedding_config(os.environ)
            embedding_profile = validated_pg_embedding_profile(
                SimpleNamespace(model=cfg["model"], size=cfg["dim"])
            )
        except Exception:
            embedding_profile = ""

    # --- Session selection --------------------------------------------------
    explicit_session_ids = _explicit_session_ids(args.session_id_hashes)
    if explicit_session_ids is None:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "error": "invalid_session_id_hash",
                    "reason": "--session-id-hash must be a sha256:<64-hex> session_id_hash",
                    "dry_run": bool(args.dry_run),
                    "selected": 0,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                    "sessions": [],
                },
                sort_keys=True,
            )
        )
        return 2
    if explicit_session_ids:
        # Event-driven path: the caller already knows which sessions changed.
        # The selection checkpoint is neither read nor written here; the
        # regular cycle stays its single writer.
        sessions = [{"session_id_hash": value} for value in explicit_session_ids]
    else:
        sessions = _select_sessions_for_cycle(
            store,
            limit=args.limit,
            project=str(args.project or ""),
            provider=str(args.provider or ""),
            backend=backend,
            mode=os.environ.get("SESSION_MEMORY_SELECTION_MODE", "auto"),
            embedding_profile=embedding_profile,
            representation=representation,
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
    # Fail-closed: 오타 등으로 알 수 없는 backend가 들어오면 조용히 fallback하지 않고
    # 명시적으로 거부한다.
    if backend != "postgres_pgvector":
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "error": "env_invalid",
                    "reason": (
                        "SESSION_MEMORY_PROJECTION_BACKEND는 'postgres_pgvector'여야 한다"
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
    if backend == "postgres_pgvector":
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
        projector_profile = str(getattr(projector, "embedding_profile", "") or "")
        if projector_profile and projector_profile != embedding_profile:
            _close_if_supported(projector)
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "embedding_profile_mismatch",
                        "reason": "PG projector embedding profile does not match the resolved active profile",
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
    projected = 0
    failed = 0
    skipped = 0
    skipped_inconsistent = 0
    identity_conflicts_auto_resolved = 0
    segments_total = 0
    segments_reused = 0
    segments_embedded = 0
    session_reports: list[dict] = []

    try:
        for session in sessions:
            session_id_hash = str(session.get("session_id_hash") or "")
            if not session_id_hash:
                skipped += 1
                continue
            try:
                if hasattr(projector, "last_local_identity_resolution"):
                    projector.last_local_identity_resolution = ""
                    projector.last_local_identity_bypassed_rows = 0
                result = materialize_and_project(
                    session_id_hash=session_id_hash,
                    store=store,
                    projector=projector,
                    backend=backend,
                )
                projection = result.get("projection") or {}
                status = str(projection.get("status") or "")
                if status == "projected":
                    projected += 1
                    outcome = "projected"
                elif status == "pending" or not result.get("fully_materialized"):
                    skipped += 1
                    outcome = "pending"
                    if str(projection.get("reason") or "") == INCONSISTENT_HERMES_PROJECT_SCOPE:
                        skipped_inconsistent += 1
                else:
                    failed += 1
                    outcome = "failed"
                session_reports.append(
                    {"session_id_hash": session_id_hash, "status": outcome, "error_class": ""}
                )
            except Exception as exc:
                failed += 1
                session_reports.append(
                    {
                        "session_id_hash": session_id_hash,
                        "status": "failed",
                        "error_class": type(exc).__name__,
                    }
                )
            if (
                str(getattr(projector, "last_local_identity_resolution", "") or "")
                == "new_local_v1_over_unverifiable_legacy"
            ):
                identity_conflicts_auto_resolved += 1
            segments_total += int(getattr(projector, "last_segments_total", 0) or 0)
            segments_reused += int(getattr(projector, "last_segments_reused", 0) or 0)
            segments_embedded += int(getattr(projector, "last_segments_embedded", 0) or 0)
            # Release per-session projection allocations across iterations
            result = None
    finally:
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
                "skipped_inconsistent": skipped_inconsistent,
                "identity_conflicts_auto_resolved": identity_conflicts_auto_resolved,
                "segments_total": segments_total,
                "segments_reused": segments_reused,
                "segments_embedded": segments_embedded,
                "sessions": session_reports,
            },
            sort_keys=True,
        )
    )
    return 0 if failed == 0 else 1


def _explicit_session_ids(values) -> list[str] | None:
    """Normalise ``--session-id-hash`` values to the stored ``sha256:<lowercase hex>``
    form (a bare 64-hex digest is accepted); None when any value is malformed."""
    from ..session_memory.change_events import normalize_session_id_hash

    ordered: list[str] = []
    for raw in values or ():
        for part in str(raw or "").split(","):
            if not part.strip():
                continue
            value = normalize_session_id_hash(part)
            if value is None:
                return None
            if value not in ordered:
                ordered.append(value)
    return ordered


def _close_if_supported(resource) -> None:
    closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        return


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
    from ..rag_ingress.embedding_provider import build_openai_embedding_provider, resolve_embedding_config
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
        return PgSessionMemoryProjector(
            store=store,
            embed_provider=provider,
            chunk_embedder=chunks,
            representation=str(environ.get("SESSION_MEMORY_REPRESENTATION") or "session_v1"),
            segment_max_bytes=int(environ.get("SESSION_MEMORY_SEGMENT_MAX_BYTES", "4096")),
        )
    except Exception:
        _close_if_supported(provider)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
