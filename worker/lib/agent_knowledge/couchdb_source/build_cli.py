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

import hashlib
import argparse
import copy
import base64
import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from ..ledger import Ledger
from ..session_memory.terminal_skipped_quarantine import record_terminal_skipped_audit
from agent_knowledge.pipeline_progress import emit_progress
from agent_knowledge.rag_ingress.pg_embedding_chunks import (
    INCONSISTENT_HERMES_PROJECT_SCOPE,
    LOCAL_EMBEDDING_DEAD_LETTER,
    PG_INPUT_POLICY_VERSION,
    TERMINAL_INPUT_REASONS,
)

BUILD_CLI_SCHEMA_VERSION = "couchdb_session_memory_build.v1"


def _safe_reason_code(value: object) -> str:
    """Closed identifier for log evidence; never carries text or session ids."""
    code = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")[:48]
    return code or "unspecified"


def _record_terminal_input_skip(
    *,
    session_id_hash: str,
    provider: str,
    project: str,
    reason: str,
    source_hash_present: bool = False,
) -> bool:
    """Best-effort audit write; a configured ledger is required."""
    ledger_path = str(os.environ.get("LLM_BRAIN_LEDGER_PATH", "") or "")
    if not ledger_path:
        return False
    return record_terminal_skipped_audit(
        Ledger(Path(ledger_path)),
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        reason=reason,
        source_hash_present=source_hash_present,
    )

class ProjectionDeadLetterLookupError(RuntimeError):
    """The local dead-letter state needed for terminal suppression is unavailable."""


BUILD_CLI_OPERATION = "couchdb_session_memory_build"
# Pending debt must not monopolise every cycle: a session that stays pending
# across this many selections is dropped from the pending set so fresh work can
# proceed (no-progress guard). Its change-feed entry re-enters if the source
# still differs from the projected revision.
MAX_PENDING_ATTEMPTS = 6


def _shard_config(
    shard_index: int | str | None = None,
    shard_count: int | str | None = None,
    environ=None,
) -> tuple[int, int]:
    """Resolve shard options; default (0, 1) preserves the legacy single lane."""
    env = os.environ if environ is None else environ
    try:
        index = int(shard_index if shard_index is not None else env.get("SESSION_MEMORY_BUILD_SHARD_INDEX", "0"))
        count = int(shard_count if shard_count is not None else env.get("SESSION_MEMORY_BUILD_SHARD_COUNT", "1"))
    except (TypeError, ValueError):
        raise ValueError("invalid_session_memory_build_shard") from None
    if count < 1 or index < 0 or index >= count:
        raise ValueError("invalid_session_memory_build_shard")
    return index, count


def _session_in_shard(session_id_hash: str, shard_index: int, shard_count: int) -> bool:
    """Stable deterministic shard membership from the canonical session id."""
    if shard_count <= 1:
        return True
    digest = hashlib.sha256(str(session_id_hash).encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % shard_count == shard_index


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
    dead_letter_lookup=None,
) -> str:
    """Return the terminal-failure source hash for a top-level state document.

    This must be called on the raw projection_state, before ``backend_receipts``
    is merged over it. For an already-projected session the receipt's
    ``projection_status`` is ``projected`` and would otherwise hide the
    top-level failed attempt.
    """
    if backend != "postgres_pgvector":
        return ""
    from .document_model import ProjectionStatus

    lane = _pg_receipt_lane(embedding_profile)
    attempt = (state.get("projection_attempt") or {}).get(lane) or {}
    if (
        lane == "postgres_pgvector_local"
        and str(attempt.get("status") or "") == ProjectionStatus.FAILED
        and str(attempt.get("reason") or "") == LOCAL_EMBEDDING_DEAD_LETTER
        and str(attempt.get("source_hash") or "")
    ):
        # A dead letter is terminal only while it still exists for a current
        # chunk. Requeue/completion must release the block for the same source.
        from ..model_connectors import LOCAL_EMBEDDING_PROFILE_ID

        session_id_hash = str(state.get("session_id_hash") or "")
        if not session_id_hash:
            raise ProjectionDeadLetterLookupError(
                "local dead-letter state is missing session_id_hash"
            )
        if dead_letter_lookup is None:
            raise ProjectionDeadLetterLookupError(
                "local dead-letter lookup unavailable"
            )
        try:
            still_blocked = bool(
                dead_letter_lookup.has_dead_letter_local_outbox_for_session(
                    session_id_hash=session_id_hash,
                    profile_id=embedding_profile or LOCAL_EMBEDDING_PROFILE_ID,
                )
            )
        except ProjectionDeadLetterLookupError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed, but surface explicitly
            raise ProjectionDeadLetterLookupError(
                "local dead-letter lookup failed"
            ) from exc
        return str(attempt["source_hash"]) if still_blocked else ""
    if _pg_receipt_representation(
        state, embedding_profile=embedding_profile
    ) != representation:
        return ""
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


def _select_sessions_needing_projection(
    store,
    limit: int,
    *,
    project: str = "",
    provider: str = "",
    backend: str = "postgres_pgvector",
    embedding_profile: str = "",
    representation: str = "session_v1",
    dead_letter_lookup=None,
    ready_content_lookup=None,
    shard_index: int = 0,
    shard_count: int = 1,
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
    from .source_revision import (
        SourceRevisionResolutionError,
        iter_active_source_hashes,
        iter_legacy_source_hashes,
        resolve_active_source_revision_from_snapshot,
        resolve_source_hash_for_currentness,
    )

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
                blocked = _blocked_source_hash_from_state(
                    state,
                    backend,
                    embedding_profile=embedding_profile,
                    representation=representation,
                    dead_letter_lookup=dead_letter_lookup,
                )
                if blocked:
                    blocked_source_hashes[session_id_hash] = blocked
        states = [
            {**state, **_pg_receipt(state, embedding_profile=embedding_profile)}
            for state in raw_states
        ]
    else:
        states = raw_states
    state_by_id = {
        str(state.get("session_id_hash") or ""): state
        for state in states
        if str(state.get("session_id_hash") or "")
    }
    projected_source_hashes = {
        session_id_hash: str(state.get("projected_source_hash") or "")
        for state in states
        if (session_id_hash := str(state.get("session_id_hash") or ""))
        and str(state.get("_id") or "") == projection_state_doc_id(session_id_hash)
        and str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
    }
    projected_representations = {
        session_id_hash: _pg_receipt_representation(
            state, embedding_profile=embedding_profile
        )
        for state in states
        if (session_id_hash := str(state.get("session_id_hash") or ""))
        and str(state.get("_id") or "") == projection_state_doc_id(session_id_hash)
        and str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
    }
    legacy_ready = (
        _legacy_ready_projection_sessions(store, states)
        if backend == "postgres_pgvector"
        else set()
    )
    use_bulk_hashes = bool(getattr(store, "base_url", ""))
    active_hashes: dict[str, str] = {}
    legacy_hashes: dict[str, str] = {}
    invalid_active: set[str] = set()
    active_scope: dict[str, dict[str, str]] = {}
    probe_session = None
    if use_bulk_hashes:
        # Paged control scans replace a find_by_session query per session.
        active_hashes, invalid_active = iter_active_source_hashes(store)
        legacy_hashes = iter_legacy_source_hashes(store)
        for pointer in store.iter_by_type(
            SourceDocType.ACTIVE_SOURCE_REVISION,
            fields=["session_id_hash", "project", "provider"],
            page_size=256,
        ):
            sid = str(pointer.get("session_id_hash") or "")
            if sid:
                active_scope[sid] = {
                    "project": str(pointer.get("project") or ""),
                    "provider": str(pointer.get("provider") or ""),
                }
        probe_session = next(iter(active_hashes), None)
    probe_done = False

    selected: list[dict] = []
    canonical_session_ids: set[str] = set()
    current_only_session_ids: list[str] = []
    current_only_seen: set[str] = set()
    unresolved_skipped = 0
    last_unresolved_reason = ""
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
        if not _session_in_shard(session_id_hash, shard_index, shard_count):
            continue
        if str(session.get("_id") or "") != session_doc_id(session_id_hash):
            if session_id_hash not in current_only_seen:
                current_only_seen.add(session_id_hash)
                current_only_session_ids.append(session_id_hash)
            continue
        canonical_session_ids.add(session_id_hash)
        projected_source_hash = projected_source_hashes.get(session_id_hash, "")
        if use_bulk_hashes:
            has_bulk_hash = (
                session_id_hash in active_hashes
                or session_id_hash in legacy_hashes
            )
            current_source_hash = active_hashes.get(session_id_hash) or legacy_hashes.get(session_id_hash, "")
            if (
                session_id_hash in invalid_active
                or not has_bulk_hash
                or (not probe_done and (session_id_hash == probe_session or probe_session is None))
            ):
                probe_done = True
                try:
                    current_source_hash = resolve_source_hash_for_currentness(
                        session_id_hash=session_id_hash, store=store
                    )
                except SourceRevisionResolutionError as exc:
                    unresolved_skipped += 1
                    last_unresolved_reason = _safe_reason_code(type(exc).__name__)
                    continue
            if session_id_hash in active_hashes:
                scope = active_scope.get(session_id_hash, {})
                if (
                    (project and scope.get("project", "") != project)
                    or (provider and scope.get("provider", "") != provider)
                ):
                    continue
        else:
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
            or (
                backend == "postgres_pgvector"
                and projected_representations.get(session_id_hash, "session_v1")
                != representation
            )
        ):
            if (
                backend == "postgres_pgvector"
                and (session_id_hash, current_source_hash) in legacy_ready
            ):
                continue
            state = state_by_id.get(session_id_hash, {})
            if (
                backend == "postgres_pgvector"
                and projected_source_hash
                and projected_source_hash != current_source_hash
                and str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
                and _ready_content_matches(
                    store,
                    ready_content_lookup,
                    state,
                    session_id_hash=session_id_hash,
                    project=project,
                    provider=provider,
                )
                and _repair_projected_source_hash(
                    store, state, session_id_hash, current_source_hash
                )
            ):
                continue
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
            or (
                backend == "postgres_pgvector"
                and projected_representations.get(session_id_hash, "session_v1")
                != representation
            )
        ):
            if (
                backend == "postgres_pgvector"
                and (session_id_hash, resolved.source_hash) in legacy_ready
            ):
                continue
            state = state_by_id.get(session_id_hash, {})
            if (
                backend == "postgres_pgvector"
                and projected_source_hash
                and projected_source_hash != resolved.source_hash
                and str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
                and _ready_content_matches(
                    store,
                    ready_content_lookup,
                    state,
                    session_id_hash=session_id_hash,
                    project=project,
                    provider=provider,
                )
                and _repair_projected_source_hash(
                    store, state, session_id_hash, resolved.source_hash
                )
            ):
                continue
            if blocked_source_hashes.get(session_id_hash) == resolved.source_hash:
                continue
            selected.append(session)
    if unresolved_skipped:
        print(
            f"[selection] unresolved_skipped={unresolved_skipped} last_reason={last_unresolved_reason}",
            file=sys.stderr,
        )
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


def _selection_checkpoint_path(shard_index: int = 0, shard_count: int = 1) -> str:
    base = os.environ.get(
        "SESSION_MEMORY_SELECTION_CHECKPOINT", _DEFAULT_SELECTION_CHECKPOINT
    )
    if shard_count > 1:
        return f"{base}.shard-{shard_index}-{shard_count}"
    return base


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
    exhausted_session_ids: dict[str, str] | None = None,
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
    if exhausted_session_ids:
        payload["exhausted_session_ids"] = {
            str(key): str(value) for key, value in exhausted_session_ids.items() if str(key)
        }
    # A full walk that stopped at its cap has not examined the remaining tail.
    # Record that instead of claiming the database-wide sequence as seen, so the
    # next cycle repeats the full walk rather than switching to an incremental
    # change feed that will never emit the untouched tail.
    if full_walk_in_progress:
        payload["full_walk_in_progress"] = True
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)


def _has_legacy_ready_projection_evidence(
    state: dict,
    documents: list[dict],
    session_id_hash: str,
    current_source_hash: str,
) -> bool:
    """Legacy projected receipts may omit content hashes.

    Accept a completed session when its state is projected, a PG receipt exists,
    the current source revision has a coverage manifest, and at least one ready
    conversation chunk carries content from that exact source revision.
    """
    from .document_model import coverage_manifest_doc_id
    from .document_model import ProjectionStatus

    if str(state.get("projection_status") or "") != ProjectionStatus.PROJECTED:
        return False
    receipts = state.get("backend_receipts")
    if not isinstance(receipts, dict) or not isinstance(receipts.get("postgres_pgvector"), dict):
        return False
    if str(state.get("projected_source_hash") or ""):
        return False
    if not current_source_hash:
        return False
    coverage = next(
        (
            document
            for document in documents
            if str(document.get("_id") or "") == coverage_manifest_doc_id(session_id_hash)
        ),
        None,
    )
    if coverage is None or str(coverage.get("source_hash") or "") != current_source_hash:
        return False
    if int(coverage.get("conversation_chunk_count") or 0) < 1:
        return False
    return any(
        str(document.get("doc_type") or "") == "conversation_chunk"
        and str(document.get("content_hash") or "")
        for document in documents
    )


def _legacy_ready_projection_sessions(store, states: list[dict]) -> set[tuple[str, str]]:
    """Bulk variant of :func:`_has_legacy_ready_projection_evidence`."""
    from .document_model import SourceDocType
    from .document_model import ProjectionStatus

    projected = {
        str(state.get("session_id_hash") or "")
        for state in states
        if str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
        and isinstance(state.get("backend_receipts"), dict)
        and isinstance(state["backend_receipts"].get("postgres_pgvector"), dict)
        and not str(state.get("projected_source_hash") or "")
    }
    result: set[tuple[str, str]] = set()
    for document in _iter_by_type(
        store,
        SourceDocType.COVERAGE_MANIFEST,
        fields=["session_id_hash", "source_hash", "conversation_chunk_count"],
        selector={},
        page_size=10000,
    ):
        session_id_hash = str(document.get("session_id_hash") or "")
        source_hash = str(document.get("source_hash") or "")
        if (
            session_id_hash in projected
            and source_hash
            and int(document.get("conversation_chunk_count") or 0) > 0
        ):
            result.add((session_id_hash, source_hash))
    return result



def _ready_content_matches(
    store,
    ready_content_lookup,
    state: dict,
    *,
    session_id_hash: str,
    project: str,
    provider: str,
) -> bool:
    del store
    if ready_content_lookup is None:
        print(
            "[selection] ready_check lookup=0 hash_source=skipped hash_present=0 "
            "chunk_id_present=0 match=0",
            file=sys.stderr,
        )
        return False
    receipts = state.get("backend_receipts")
    pg = receipts.get("postgres_pgvector") if isinstance(receipts, dict) else None
    pg = pg if isinstance(pg, dict) else {}
    chunk_id = str(pg.get("session_memory_knowledge_id") or "")
    content_hash = str(state.get("active_content_hash") or "")
    hash_source = "active"
    if not content_hash:
        content_hash = str(pg.get("representation_content_hash") or "")
        hash_source = "receipt"
    match = False
    if content_hash:
        try:
            match = bool(
                ready_content_lookup(
                    session_id_hash=session_id_hash,
                    project=project,
                    provider=provider,
                    content_hash=content_hash,
                    chunk_id=chunk_id,
                )
            )
        except Exception:
            match = False
    print(
        f"[selection] ready_check lookup=1 hash_source={hash_source} "
        f"hash_present={int(bool(content_hash))} "
        f"chunk_id_present={int(bool(chunk_id))} match={int(match)}",
        file=sys.stderr,
    )
    return match


def _repair_projected_source_hash(
    store,
    state: dict,
    session_id_hash: str,
    current_source_hash: str,
) -> bool:
    """Repair a stale source-hash pointer after verifying identical content."""
    repaired = copy.deepcopy(state)
    repaired["projected_source_hash"] = current_source_hash
    repaired["source_hash_repaired_at"] = datetime.now(timezone.utc).isoformat()
    receipts = repaired.get("backend_receipts")
    if isinstance(receipts, dict) and isinstance(receipts.get("postgres_pgvector"), dict):
        receipts["postgres_pgvector"]["projected_source_hash"] = current_source_hash
    try:
        result = store.put(repaired)
        return str(getattr(result, "outcome", "")) != "conflict"
    except Exception:
        return False


def _recorded_source_hash(store, session_id_hash: str) -> str:
    from .document_model import projection_state_doc_id

    state = store.get(projection_state_doc_id(session_id_hash)) or {}
    return str(state.get("source_hash") or "")

def _evaluate_session_change(
    store,
    session_id_hash: str,
    *,
    project: str,
    provider: str,
    embedding_profile: str = "",
    representation: str = "session_v1",
    dead_letter_lookup=None,
    ready_content_lookup=None,
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
            dead_letter_lookup=dead_letter_lookup,
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
        if (
            projected_source_hash
            and projected_source_hash != current_source_hash
            and str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
            and _ready_content_matches(
                store,
                ready_content_lookup,
                merged,
                session_id_hash=session_id_hash,
                project=project,
                provider=provider,
            )
            and _repair_projected_source_hash(
                store, state, session_id_hash, current_source_hash
            )
        ):
            return False, None
        if _has_legacy_ready_projection_evidence(
            merged, documents, session_id_hash, current_source_hash
        ):
            return False, None
        return True, canonical if canonical is not None else resolved.sessions[0]
    # A source revision can be "current" in CouchDB while its PG ready row is
    # still missing (for example after an interrupted projection). Such a session
    # must stay selectable so the PG gap is closed instead of being treated as
    # already projected.
    # Ignore a stale CouchDB receipt chunk id here: the PG row may still exist
    # under the session identity even when the stored chunk id no longer resolves.
    pg_missing = True
    if ready_content_lookup is not None:
        try:
            pg_missing = not bool(
                ready_content_lookup(
                    session_id_hash=session_id_hash,
                    project=project,
                    provider=provider,
                    content_hash=str(merged.get("active_content_hash") or merged.get("projected_source_hash") or ""),
                    chunk_id="",
                )
            )
        except Exception:
            pg_missing = True
    if ready_content_lookup is not None and pg_missing:
        return True, canonical if canonical is not None else (
            resolved.sessions[0] if getattr(resolved, "sessions", None) else {"session_id_hash": session_id_hash}
        )
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
    dead_letter_lookup=None,
    ready_content_lookup=None,
    shard_index: int = 0,
    shard_count: int = 1,
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
        if not _session_in_shard(session_id_hash, shard_index, shard_count):
            current_watermark = seq
            continue
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
            dead_letter_lookup=dead_letter_lookup,
            ready_content_lookup=ready_content_lookup,
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
    dead_letter_lookup=None,
    ready_content_lookup=None,
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
                dead_letter_lookup=dead_letter_lookup,
                ready_content_lookup=ready_content_lookup,
            )
        except ProjectionDeadLetterLookupError:
            raise
        except Exception:  # noqa: BLE001 - a read failure must not discard pending work
            still_pending.append({"session_id_hash": session_id_hash})
            continue
        if needs_projection and session is not None:
            still_pending.append(session)
    return still_pending



def _pending_state_session_ids(
    store,
    *,
    limit: int,
    project: str,
    provider: str,
    ready_content_lookup,
    exhausted: dict,
    shard_index: int = 0,
    shard_count: int = 1,
) -> list[str]:
    """Find state-pending sessions whose CouchDB chunks lack a PG ready row.

    Incremental selection only visits the checkpoint and the change feed, so a
    projection_state document that stayed pending across a completed full walk
    is never revisited. This bounded sweep closes that gap without a DB DSN.
    """
    from .document_model import SourceDocType

    selector = {"projection_status": "pending"}
    if project:
        selector["project"] = project
    if provider:
        selector["provider"] = provider
    # Bound the sweep. The previous ``limit=0`` sharded path materialised every
    # pending projection_state once per shard (three full scans on the measured
    # deployment). A bounded pass inspects at most ``limit * shard_count``
    # states; the sweep is a gap-filler behind the checkpoint/change-feed
    # selection, so it must not dominate a cycle.
    scan_limit = 0
    if limit > 0:
        scan_limit = int(limit) * (max(1, int(shard_count)) if shard_count > 1 else 1)
    states = store.iter_by_type(
        SourceDocType.PROJECTION_STATE,
        fields=["_id", "session_id_hash", "active_content_hash", "backend_receipts"],
        selector=selector,
        limit=scan_limit,
        page_size=256,
    )
    selected: list[str] = []
    scanned = 0
    chunked = 0
    for state in states:
        sid = str(state.get("session_id_hash") or "")
        if not sid.startswith("sha256:") or sid in exhausted:
            continue
        if not _session_in_shard(sid, shard_index, shard_count):
            continue
        scanned += 1
        chunk_docs = store.find_by_type(
            SourceDocType.CONVERSATION_CHUNK,
            fields=["session_id_hash"],
            selector={"session_id_hash": sid},
            limit=1,
        )
        if not chunk_docs:
            continue
        chunked += 1
        active_hash = str(state.get("active_content_hash") or "")
        receipts = state.get("backend_receipts")
        pg = receipts.get("postgres_pgvector") if isinstance(receipts, dict) else None
        chunk_id = str(pg.get("session_memory_knowledge_id") or "") if isinstance(pg, dict) else ""
        if not active_hash:
            continue
        try:
            exists = bool(
                ready_content_lookup(
                    session_id_hash=sid,
                    project=project,
                    provider=provider,
                    content_hash=active_hash,
                    chunk_id=chunk_id,
                )
            )
        except Exception:
            continue
        if exists:
            continue
        selected.append(sid)
        if limit > 0 and len(selected) >= limit:
            break
    print(
        f"[selection] pending_state_sweep scanned={scanned} chunked={chunked} "
        f"missing_pg={len(selected)}",
        file=sys.stderr,
    )
    return selected
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
    dead_letter_lookup=None,
    persist: bool = True,
    ready_content_lookup=None,
    shard_index: int = 0,
    shard_count: int = 1,
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
    path = _selection_checkpoint_path(shard_index, shard_count)
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
    pending_ids = [
        sid for sid in _pending_session_ids(checkpoint)
        if _session_in_shard(sid, shard_index, shard_count)
    ]
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

    exhausted_before_sweep = dict(checkpoint.get("exhausted_session_ids") or {})
    if (
        backend == "postgres_pgvector"
        and ready_content_lookup is not None
        and normalized != "full"
    ):
        sweep_limit = limit if limit > 0 else 0
        swept = _pending_state_session_ids(
            store,
            limit=sweep_limit,
            project=project,
            provider=provider,
            ready_content_lookup=ready_content_lookup,
            exhausted=exhausted_before_sweep,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        pending_ids = list(dict.fromkeys([*pending_ids, *swept]))
    pending_sessions = _resolve_pending_sessions(
        store,
        pending_ids,
        project=project,
        provider=provider,
        embedding_profile=embedding_profile,
        representation=representation,
        dead_letter_lookup=dead_letter_lookup,
        ready_content_lookup=ready_content_lookup,
    )
    pending_attempts = dict(checkpoint.get("pending_session_attempts") or {})
    exhausted = dict(checkpoint.get("exhausted_session_ids") or {})
    for identity, recorded_hash in list(exhausted.items()):
        current_hash = _recorded_source_hash(store, identity)
        if current_hash and current_hash != recorded_hash:
            exhausted.pop(identity, None)
            pending_attempts.pop(identity, None)
    attempts_before = set(pending_attempts)
    pending_sessions, pending_attempts, dropped_pending = _apply_pending_attempt_cap(
        pending_sessions, pending_attempts
    )
    if dropped_pending:
        print(
            f"[selection] pending_attempts_exhausted dropped={dropped_pending}",
            file=sys.stderr,
        )
    exhausted_identities = attempts_before - set(pending_attempts)
    for identity in exhausted_identities:
        exhausted[identity] = _recorded_source_hash(store, identity)
        pending_attempts[identity] = MAX_PENDING_ATTEMPTS
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
                dead_letter_lookup=dead_letter_lookup,
                ready_content_lookup=ready_content_lookup,
                shard_index=shard_index,
                shard_count=shard_count,
            )
        except ProjectionDeadLetterLookupError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-safe fallback to the full walk
            print(
                f"[selection] incremental fallback ({type(exc).__name__})",
                file=sys.stderr,
            )
        else:
            fresh = [
                session
                for session in fresh
                if _session_identity(session) not in exhausted
            ]
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
            dead_letter_lookup=dead_letter_lookup,
            ready_content_lookup=ready_content_lookup,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        fresh = [
            session
            for session in fresh
            if _session_identity(session) not in exhausted
        ]
        sessions = _dedupe_sessions(processed_pending + fresh)
        capped = fresh_limit is not None and fresh_limit > 0 and len(fresh) >= fresh_limit
        next_full_walk_pending = capped
        next_seq = "" if capped else sequence_before
        print(
            f"[selection] mode=full selected={len(fresh)} "
            f"pending_recovered={len(processed_pending)} capped={str(capped).lower()}",
            file=sys.stderr,
        )

    sessions = [
        session for session in sessions
        if _session_in_shard(_session_identity(session), shard_index, shard_count)
    ]
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
                    if key in set(durable_pending) or key in exhausted
                },
                exhausted_session_ids=exhausted,
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




def _persist_post_projection_pending(session_reports: list[dict], shard_index: int = 0, shard_count: int = 1) -> None:
    """Drop successfully projected sessions from pending immediately.

    Failures and unfinished sessions stay pending; exhausted identities remain
    suppressed. This keeps the next cycle from reselecting completed work.
    """
    path = _selection_checkpoint_path(shard_index, shard_count)
    checkpoint = _read_selection_checkpoint(path)
    settled = {
        str(row.get("session_id_hash") or "")
        for row in session_reports
        if str(row.get("status") or "") in {"projected", "terminal_skipped"}
    }
    terminal = {
        str(row.get("session_id_hash") or "")
        for row in session_reports
        if str(row.get("status") or "") == "terminal_skipped"
    }
    pending = [sid for sid in _pending_session_ids(checkpoint) if sid not in settled]
    pending_set = set(pending)
    attempts = {
        key: value
        for key, value in (checkpoint.get("pending_session_attempts") or {}).items()
        if key in pending_set
    }
    exhausted = {
        key: value
        for key, value in (checkpoint.get("exhausted_session_ids") or {}).items()
        if key in pending_set
    }
    exhausted.update({sid: "terminal_input_reason" for sid in terminal if sid})
    try:
        _write_selection_checkpoint(
            path,
            str(checkpoint.get("last_seq") or ""),
            pending_session_ids=pending,
            full_walk_in_progress=bool(checkpoint.get("full_walk_in_progress")),
            embedding_profile=str(checkpoint.get("embedding_profile") or ""),
            representation=str(checkpoint.get("representation") or "session_v1"),
            pending_attempts=attempts,
            exhausted_session_ids=exhausted,
        )
    except OSError:
        pass
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge couchdb-session-memory-build",
        description="Build CouchDB->session-memory live pipeline.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report selection counts; no RetiredIndexBridge writes.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum sessions to process (0 = unlimited).")
    parser.add_argument("--project", default="", help="Scope selection to this project (empty = all).")
    parser.add_argument("--provider", default="", help="Scope selection to this provider (empty = all).")
    parser.add_argument(
        "--shard-index",
        default=os.environ.get("SESSION_MEMORY_BUILD_SHARD_INDEX", "0"),
        help="Zero-based shard index (default 0; env SESSION_MEMORY_BUILD_SHARD_INDEX).",
    )
    parser.add_argument(
        "--shard-count",
        default=os.environ.get("SESSION_MEMORY_BUILD_SHARD_COUNT", "1"),
        help="Number of build shards (default 1 = no sharding; env SESSION_MEMORY_BUILD_SHARD_COUNT).",
    )
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
    try:
        shard_index, shard_count = _shard_config(args.shard_index, args.shard_count)
    except ValueError:
        print(
            json.dumps(
                {
                    "error": "invalid_shard_config",
                    "reason": "--shard-index/--shard-count must satisfy 0 <= index < count and count >= 1",
                },
                sort_keys=True,
            )
        )
        return 2

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
        except (ValueError, KeyError, TypeError):
            embedding_profile = ""

    dead_letter_lookup = None
    if backend == "postgres_pgvector":
        try:
            dead_letter_lookup = _build_pg_dead_letter_lookup(os.environ)
        except Exception:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "dead_letter_lookup_unavailable",
                        "reason": "local dead-letter lookup could not be constructed",
                        "dry_run": bool(args.dry_run),
                        "selected": 0,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2

    ready_content_lookup = None
    if dead_letter_lookup is not None and hasattr(
        dead_letter_lookup, "find_ready_chunk_by_identity"
    ):
        from ..model_connectors import DEFAULT_EMBEDDING_PROFILE_ID

        lookup_store = dead_letter_lookup
        lookup_profile = str(embedding_profile or DEFAULT_EMBEDDING_PROFILE_ID)

        def ready_content_lookup(
            *,
            session_id_hash: str,
            project: str,
            provider: str,
            content_hash: str,
            chunk_id: str = "",
        ) -> bool:
            if not content_hash:
                return False
            try:
                if chunk_id:
                    row = lookup_store.get_chunk(chunk_id)
                    return bool(
                        row is not None
                        and str(getattr(row, "content_hash", "") or "") == content_hash
                        and str(getattr(row, "embedding_state", "") or "") == "ready"
                    )
                return (
                    lookup_store.find_ready_chunk_by_identity(
                        session_id_hash=session_id_hash,
                        project=project,
                        provider=provider,
                        content_hash=content_hash,
                        embedding_model=lookup_profile,
                    )
                    is not None
                )
            except Exception:
                return False

    # --- Session selection --------------------------------------------------
    # A single start marker lets the checker distinguish an instrumented image
    # from one that predates progress events; it is not a repeated heartbeat.
    emit_progress("selection", 0, 0)
    explicit_session_ids = _explicit_session_ids(args.session_id_hashes)
    embedding_cycle_cap_enabled = _embedding_cycle_cap_enabled(os.environ)
    embedding_budget = None
    effective_limit = args.limit
    if (
        embedding_cycle_cap_enabled
        and backend == "postgres_pgvector"
        and not args.dry_run
        and not explicit_session_ids
    ):
        cycle_chunks, per_session_chunks = _embedding_cycle_budget(os.environ)
        embedding_budget = dict(cycle_chunks=cycle_chunks, per_session_chunks=per_session_chunks)
        session_limit = max(1, cycle_chunks // per_session_chunks)
        effective_limit = min(args.limit, session_limit) if args.limit > 0 else session_limit
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
    try:
        if explicit_session_ids:
            # Event-driven path: the caller already knows which sessions changed.
            # The selection checkpoint is neither read nor written here; the
            # regular cycle stays its single writer.
            sessions = [{"session_id_hash": value} for value in explicit_session_ids]
        else:
            sessions = _select_sessions_for_cycle(
                store,
                limit=effective_limit,
                project=str(args.project or ""),
                provider=str(args.provider or ""),
                backend=backend,
                mode=os.environ.get("SESSION_MEMORY_SELECTION_MODE", "auto"),
                embedding_profile=embedding_profile,
                representation=representation,
                dead_letter_lookup=dead_letter_lookup,
                persist=not args.dry_run,
                ready_content_lookup=ready_content_lookup,
                shard_index=shard_index,
                shard_count=shard_count,
            )
    except ProjectionDeadLetterLookupError as exc:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "error": "dead_letter_lookup_unavailable",
                    "reason": str(exc),
                    "dry_run": bool(args.dry_run),
                    "selected": 0,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        _close_if_supported(dead_letter_lookup)
    selected_count = len(sessions)
    if not args.dry_run:
        emit_progress("selection", selected_count, selected_count)

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
        if (
            embedding_cycle_cap_enabled
            and selected_count <= 1
            and (explicit_session_ids or str(args.project or ""))
        ):
            exempt = getattr(projector, "exempt_from_cycle_budget", None)
            if callable(exempt):
                exempt()
    projected = 0
    failed = 0
    skipped = 0
    skipped_inconsistent = 0
    identity_conflicts_auto_resolved = 0
    segments_total = 0
    segments_reused = 0
    segments_embedded = 0
    terminal_skipped = 0
    terminal_audit_written = 0
    terminal_reason_counts: dict[str, int] = {}
    session_reports: list[dict] = []

    try:
        for index, session in enumerate(sessions):
            session_id_hash = str(session.get("session_id_hash") or "")
            if not session_id_hash:
                skipped += 1
                continue
            failure_reason = ""
            try:
                if hasattr(projector, "last_local_identity_resolution"):
                    projector.last_local_identity_resolution = ""
                    projector.last_local_identity_bypassed_rows = 0
                result = materialize_and_project(
                    session_id_hash=session_id_hash,
                    store=store,
                    projector=projector,
                    backend=backend,
                    progress_callback=emit_progress,
                )
                projection = result.get("projection") or {}
                status = str(projection.get("status") or "")
                reason = _safe_reason_code(projection.get("reason"))
                if reason in TERMINAL_INPUT_REASONS and (
                    status == "failed" or not result.get("fully_materialized")
                ):
                    terminal_skipped += 1
                    terminal_reason_counts[reason] = terminal_reason_counts.get(reason, 0) + 1
                    outcome = "terminal_skipped"
                    failure_reason = reason
                    if reason == INCONSISTENT_HERMES_PROJECT_SCOPE:
                        skipped_inconsistent += 1
                    if _record_terminal_input_skip(
                        session_id_hash=session_id_hash,
                        provider=str(session.get("provider") or ""),
                        project=str(session.get("project") or ""),
                        reason=reason,
                        source_hash_present=bool(projection.get("source_hash")),
                    ):
                        terminal_audit_written += 1
                elif status == "projected":
                    projected += 1
                    outcome = "projected"
                elif status == "pending" or not result.get("fully_materialized"):
                    skipped += 1
                    outcome = "pending"
                else:
                    failed += 1
                    outcome = "failed"
                    failure_reason = reason
                session_reports.append(
                    {"session_id_hash": session_id_hash, "status": outcome, "error_class": ""}
                )
            except Exception as exc:
                failed += 1
                failure_reason = _safe_reason_code(type(exc).__name__)
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
            emit_progress("projection", index + 1, selected_count, reason_code=failure_reason)
    finally:
        _close_if_supported(projector)
    if terminal_reason_counts:
        print(
            json.dumps(
                {
                    "event": "terminal_input_skip_summary",
                    "terminal_skipped": terminal_skipped,
                    "reason_counts": dict(sorted(terminal_reason_counts.items())),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )

    if (
        embedding_cycle_cap_enabled
        and selected_count == 1
        and projected == 0
        and terminal_skipped == 0
        and failed == 0
        and (explicit_session_ids or str(args.project or ""))
    ):
        failed = 1
        failure_reason = "single_session_not_projected"
        for row in session_reports:
            row.update(status="failed", error_class=failure_reason)
    report = {
        "schema_version": BUILD_CLI_SCHEMA_VERSION,
        "dry_run": False,
        "selected": selected_count,
        "projected": projected,
        "failed": failed,
        "skipped": skipped,
        "skipped_inconsistent": skipped_inconsistent,
        "terminal_skipped": terminal_skipped,
        "terminal_audit_written": terminal_audit_written,
        "terminal_skipped_reason_counts": dict(sorted(terminal_reason_counts.items())),
        "identity_conflicts_auto_resolved": identity_conflicts_auto_resolved,
        "segments_total": segments_total,
        "segments_reused": segments_reused,
        "segments_embedded": segments_embedded,
        "sessions": session_reports,
    }
    if embedding_cycle_cap_enabled:
        report["embedding_cycle_limit"] = (
            embedding_budget["cycle_chunks"] if embedding_budget else 0
        )
        report["embedding_capped"] = bool(
            embedding_budget and effective_limit < args.limit
        )
    print(json.dumps(report, sort_keys=True))
    if not args.dry_run and not explicit_session_ids:
        _persist_post_projection_pending(session_reports, shard_index, shard_count)
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


def _build_pg_dead_letter_lookup(environ):
    dsn = (
        environ.get("NEURON_LBRAIN_PGVECTOR_DSN", "")
        or environ.get("LLM_BRAIN_PGVECTOR_DSN", "")
        or environ.get("NEURON_LEDGER_PG_DSN", "")
    )
    if not dsn:
        return None
    from ..postgres_store.pgvector_store import PgVectorStore

    return PgVectorStore(
        dsn=dsn,
        pgvector_version=environ.get("LLM_BRAIN_PGVECTOR_VERSION", "0.8.0"),
    )



def _embedding_cycle_cap_enabled(environ) -> bool:
    """Cap logic is opt-in; the legacy path remains the default."""
    return str(environ.get("PG_EMBEDDING_CYCLE_CAP_ENABLED", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


DEFAULT_EMBEDDING_CYCLE_CHUNKS = 512
DEFAULT_EMBEDDING_SESSION_CHUNKS = 16
MAX_EMBEDDING_CYCLE_CHUNKS = 1000


def _embedding_cycle_budget(environ):
    """Conservative per-run embedding budget shared by full and incremental modes."""
    try:
        cycle = int(environ.get("PG_EMBEDDING_CHUNKS_PER_CYCLE",
                                str(DEFAULT_EMBEDDING_CYCLE_CHUNKS)))
        session = int(environ.get("PG_EMBEDDING_CHUNKS_PER_SESSION",
                                  str(DEFAULT_EMBEDDING_SESSION_CHUNKS)))
    except (TypeError, ValueError):
        raise ValueError("invalid_pg_embedding_budget") from None
    if cycle < 1 or session < 1:
        raise ValueError("invalid_pg_embedding_budget")
    return min(cycle, MAX_EMBEDDING_CYCLE_CHUNKS), session

def _build_pg_projector(environ):
    dsn = (
        environ.get("NEURON_LBRAIN_PGVECTOR_DSN", "")
        or environ.get("LLM_BRAIN_PGVECTOR_DSN", "")
        or environ.get("NEURON_LEDGER_PG_DSN", "")
    )
    if not dsn:
        return None
    import inspect
    from ..rag_ingress.pg_backfill import PgSessionMemoryProjector
    from ..postgres_store.pgvector_store import PgVectorStore
    from ..rag_ingress.embedding_provider import build_openai_embedding_provider, resolve_embedding_config
    from ..rag_ingress.pg_embedding_chunks import BoundedPgEmbedding

    store = PgVectorStore(dsn=dsn, pgvector_version=environ.get("LLM_BRAIN_PGVECTOR_VERSION", "0.8.0"))
    try:
        params = inspect.signature(build_openai_embedding_provider).parameters
    except (TypeError, ValueError):
        params = {}
    if "progress_callback" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        provider = build_openai_embedding_provider(environ=environ, progress_callback=emit_progress)
    else:
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
